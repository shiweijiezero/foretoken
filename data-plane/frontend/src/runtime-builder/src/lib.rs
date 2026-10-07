// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Builds complete runtime generations from immutable serving snapshots.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use foretoken_admission::AdmissionTargetState;
use foretoken_backend_registry::{
    BackendRegistry, BackendRegistryBuild, ModelIdentity, ServingSnapshot,
};
use foretoken_kv_indexer::{KvIndexDegradedReason, KvIndexer};
use foretoken_llm_facade::LlmFacadeResolver;
use foretoken_router::{
    PipelineRouter, RouteInventory, RouteTargetStatsReader, Router, RouterPipeline,
    RouterPipelineConfig, RouterPipelineConfigError,
};
use foretoken_server::{
    KvIndexDiagnostics, ModelRuntime, RuntimeBundle, RuntimeControl, RuntimeGeneration,
    RuntimeState,
};
use foretoken_text::{SnapshotRuntime, load_snapshot_runtime};
use thiserror::Error;

#[derive(Debug, Clone, Copy)]
pub enum KvIndexCredential {
    Disabled,
    Key([u8; 32]),
    Degraded(KvIndexDegradedReason),
}

struct CachedModelBundle {
    identity: ModelIdentity,
    max_model_len: u32,
    max_logprobs: Option<i32>,
    dtype: Option<String>,
    prepared_tokenizer: Option<String>,
    bundle: Arc<RuntimeBundle>,
}

type ModelBundleCache = Mutex<BTreeMap<String, CachedModelBundle>>;

pub struct RuntimeBuilder {
    router_pipeline: Arc<RouterPipeline>,
    kv_credential: KvIndexCredential,
    routing_load: foretoken_router::RoutingLoadState,
    model_bundles: ModelBundleCache,
}

impl RuntimeBuilder {
    /// Creates the snapshot builder retained by the frontend watcher for successive updates.
    ///
    /// The watcher reuses the returned builder to parse and prepare generations with this routing
    /// pipeline and KV credential for its lifetime. Invalid routing configuration fails at startup.
    pub fn new(
        router_pipeline: RouterPipelineConfig,
        kv_credential: KvIndexCredential,
    ) -> Result<Self, RouterPipelineConfigError> {
        Ok(Self {
            router_pipeline: Arc::new(router_pipeline.build()?),
            kv_credential,
            routing_load: Default::default(),
            model_bundles: Mutex::new(BTreeMap::new()),
        })
    }

    /// Decodes controller-provided bytes into a serving snapshot candidate for [`Self::build`].
    pub fn parse(&self, bytes: &[u8]) -> Result<ServingSnapshot, RuntimeBuildError> {
        Ok(serde_json::from_slice(bytes)?)
    }

    /// Prepares one publishable runtime generation from a decoded snapshot.
    ///
    /// The snapshot watcher calls this before publication. It returns a fully probed runtime whose
    /// ownership transfers to [`PreparedRuntime::publish`], or an error while the active runtime
    /// remains unchanged.
    // Build one publishable generation in stages: validate projection, construct routing and
    // KV state, probe backends, load per-model processors, re-probe, then seal PreparedRuntime.
    pub async fn build(
        &self,
        snapshot: ServingSnapshot,
    ) -> Result<PreparedRuntime, RuntimeBuildError> {
        let version = snapshot.version;
        let has_physical_backends = !snapshot.groups.is_empty()
            || !snapshot.pd_components.is_empty()
            || !snapshot.epd_components.is_empty();
        let identities = snapshot
            .model_identities()
            .map_err(|error| RuntimeBuildError::InvalidSnapshot(error.to_string()))?;
        let admission_targets = snapshot
            .admission_target_sets()
            .map_err(|error| RuntimeBuildError::InvalidSnapshot(error.to_string()))?;
        let BackendRegistryBuild {
            registry,
            kv_runtime_config,
        } = BackendRegistryBuild::from_snapshot(snapshot.clone())
            .map_err(|error| RuntimeBuildError::InvalidSnapshot(error.to_string()))?;
        let registry = Arc::new(registry);
        let kv_indexer = Arc::new(match self.kv_credential {
            KvIndexCredential::Disabled if !kv_runtime_config.event_sources.is_empty() => {
                KvIndexer::degraded(kv_runtime_config, KvIndexDegradedReason::KeyMissing)
            }
            KvIndexCredential::Disabled => KvIndexer::new(kv_runtime_config, None)
                .map_err(|error| RuntimeBuildError::InvalidSnapshot(error.to_string()))?,
            KvIndexCredential::Key(key) => KvIndexer::new(kv_runtime_config, Some(key))
                .map_err(|error| RuntimeBuildError::InvalidSnapshot(error.to_string()))?,
            KvIndexCredential::Degraded(reason) => KvIndexer::degraded(kv_runtime_config, reason),
        });
        let control = Arc::new(RegistryRuntimeControl {
            registry: registry.clone(),
            kv_indexer: kv_indexer.clone(),
            routing_load: self.routing_load.clone(),
        });
        control.refresh_backend_readiness().await;
        if has_physical_backends && !registry.is_ready() {
            return Err(RuntimeBuildError::BackendUnavailable);
        }
        let healthy_models = registry
            .healthy_models()
            .into_iter()
            .collect::<BTreeSet<_>>();
        let video_models = identities
            .iter()
            .filter(|(model, identity)| {
                healthy_models.contains(*model) && identity.capabilities.contains("video")
            })
            .map(|(model, _)| model.clone())
            .collect::<BTreeSet<_>>();
        let models = if has_physical_backends {
            model_runtimes(
                identities
                    .into_iter()
                    .filter(|(model, _)| {
                        healthy_models.contains(model) && !video_models.contains(model)
                    })
                    .collect(),
                &registry,
                &self.model_bundles,
            )
            .await?
        } else {
            BTreeMap::new()
        };

        // Preparation may be slow. Probe again so only a fully ready physical candidate is published.
        control.refresh_backend_readiness().await;
        if has_physical_backends {
            let healthy_models = registry
                .healthy_models()
                .into_iter()
                .collect::<BTreeSet<_>>();
            if !models
                .keys()
                .chain(video_models.iter())
                .all(|model| healthy_models.contains(model))
            {
                return Err(RuntimeBuildError::BackendBecameUnavailable);
            }
        }
        let router: Arc<dyn Router> = Arc::new(
            PipelineRouter::with_pipeline(registry.clone(), self.router_pipeline.clone())
                .with_load_state(self.routing_load.clone())
                .with_kv_prefix_indexer(kv_indexer)
                .with_route_target_stats_reader(registry.clone()),
        );
        let video_inventory: Arc<dyn foretoken_router::RouteInventory> = registry.clone();
        let resolver: Arc<dyn LlmFacadeResolver> = registry;
        let mut state = RuntimeState::new(models, router, resolver);
        if !video_models.is_empty() {
            let client = reqwest::Client::builder()
                .connect_timeout(std::time::Duration::from_secs(10))
                .build()
                .map_err(|error| RuntimeBuildError::ModelRuntime(error.to_string()))?;
            state = state.with_video_backend(video_models, video_inventory, client);
        }
        for (model, candidates) in admission_targets {
            state = state.with_admission_targets(model, candidates);
        }
        Ok(PreparedRuntime {
            version,
            state: Arc::new(state),
            control,
        })
    }
}

pub struct PreparedRuntime {
    version: u64,
    state: Arc<RuntimeState>,
    control: Arc<dyn RuntimeControl>,
}

impl PreparedRuntime {
    /// Atomically hands this prepared candidate to the long-lived serving generation.
    ///
    /// Snapshot watchers consume the candidate exactly once. Returns whether its version replaced
    /// the active state; stale candidates are dropped without affecting request handling.
    pub fn publish(self, generation: &RuntimeGeneration) -> bool {
        generation.replace_state(self.version, self.state, self.control)
    }
}

#[derive(Debug, Error)]
pub enum RuntimeBuildError {
    #[error("could not parse serving snapshot: {0}")]
    Parse(#[from] serde_json::Error),
    #[error("could not validate serving snapshot: {0}")]
    InvalidSnapshot(String),
    #[error("model-server backend is not ready")]
    BackendUnavailable,
    #[error("could not prepare model runtime: {0}")]
    ModelRuntime(String),
    #[error("candidate model-server backend became unready during runtime preparation")]
    BackendBecameUnavailable,
}

struct RegistryRuntimeControl {
    registry: Arc<BackendRegistry>,
    kv_indexer: Arc<KvIndexer>,
    routing_load: foretoken_router::RoutingLoadState,
}

#[async_trait]
impl RuntimeControl for RegistryRuntimeControl {
    async fn refresh_backend_readiness(&self) {
        let (_, _) = tokio::join!(
            self.registry.refresh_backend_readiness(),
            self.kv_indexer.refresh(),
        );
    }

    fn configured_models(&self) -> Vec<String> {
        self.registry.configured_models()
    }

    fn is_ready(&self) -> bool {
        self.registry.is_configured()
    }

    fn model_ready(&self, model: &str) -> bool {
        self.registry.is_model_ready(model)
    }

    fn route_target_states(
        &self,
        model: &str,
        window: std::time::Duration,
    ) -> Vec<AdmissionTargetState> {
        self.registry
            .model_routes()
            .routes()
            .iter()
            .filter(|target| target.model == model)
            .map(|target| {
                let mut target = target.clone();
                target.capabilities = self
                    .registry
                    .effective_capabilities(&target.route_target_id);
                AdmissionTargetState {
                    healthy: self
                        .registry
                        .is_route_target_healthy(&target.route_target_id),
                    statistics: self.registry.stats(&target.route_target_id, window),
                    frontend_load: (0..target.data_parallel_size)
                        .map(|rank| {
                            (
                                rank,
                                self.routing_load.snapshot(&target.route_target_id, rank),
                            )
                        })
                        .collect(),
                    target,
                }
            })
            .collect()
    }

    fn kv_index_diagnostics(&self) -> KvIndexDiagnostics {
        let status = self.kv_indexer.status();
        KvIndexDiagnostics {
            state: status.state.as_str().into(),
            reason: status.reason.map(|reason| reason.as_str().into()),
            sources_healthy: status.sources_healthy,
            sources_total: status.sources_total,
        }
    }
}

async fn model_runtimes(
    identities: BTreeMap<String, ModelIdentity>,
    registry: &BackendRegistry,
    cached_bundles: &ModelBundleCache,
) -> Result<BTreeMap<String, ModelRuntime>, RuntimeBuildError> {
    cached_bundles
        .lock()
        .expect("model runtime cache lock poisoned")
        .retain(|model, _| identities.contains_key(model));
    let mut runtimes = BTreeMap::new();
    for (model, identity) in identities {
        let max_model_len = registry.effective_max_model_len(&model).ok_or_else(|| {
            RuntimeBuildError::ModelRuntime(format!(
                "no healthy runtime metadata for model {model}"
            ))
        })?;
        let model_dtype = registry
            .effective_model_dtype(&model)
            .map_err(RuntimeBuildError::ModelRuntime)?;
        let max_logprobs = registry.effective_max_logprobs(
            &model,
            foretoken_text::backend::SamplingLimits::DEFAULT_MAX_LOGPROBS,
        );
        let dtype_key = model_dtype.map(|dtype| dtype.as_str().to_owned());
        let prepared_tokenizer = registry
            .prepared_tokenizer(&model)
            .map_err(RuntimeBuildError::ModelRuntime)?;
        let prepared_key = prepared_tokenizer
            .as_ref()
            .map(|prepared| format!("{prepared:?}"));
        if let Some(bundle) = cached_bundles
            .lock()
            .expect("model runtime cache lock poisoned")
            .get(&model)
            .filter(|cached| {
                cached.identity == identity
                    && cached.max_model_len == max_model_len
                    && cached.max_logprobs == max_logprobs
                    && cached.dtype == dtype_key
                    && cached.prepared_tokenizer == prepared_key
            })
            .map(|cached| cached.bundle.clone())
        {
            runtimes.insert(model, ModelRuntime::new(bundle));
            continue;
        }
        let SnapshotRuntime {
            text_processor,
            tokenizer,
            chat_processor,
            supports_multimodal,
        } = load_snapshot_runtime(
            identity.source,
            &identity.tokenizer,
            &identity.tokenizer_revision,
            max_model_len,
            max_logprobs,
            model_dtype,
            prepared_tokenizer.as_ref(),
        )
        .await
        .map_err(|error| RuntimeBuildError::ModelRuntime(error.to_string()))?;
        let unsupported = unsupported_media_capabilities(&identity.capabilities);
        if !unsupported.is_empty() {
            return Err(RuntimeBuildError::ModelRuntime(format!(
                "model {model} declares unsupported media capabilities: {}",
                unsupported.join(", ")
            )));
        }
        if identity
            .capabilities
            .iter()
            .any(|capability| capability == "multimodal" || capability.starts_with("multimodal."))
            && !supports_multimodal
        {
            return Err(RuntimeBuildError::ModelRuntime(format!(
                "model {model} declares multimodal routing capabilities but its frontend processor does not support image input"
            )));
        }
        let bundle = Arc::new(RuntimeBundle::new(
            text_processor,
            tokenizer,
            chat_processor,
        ));
        cached_bundles
            .lock()
            .expect("model runtime cache lock poisoned")
            .insert(
                model.clone(),
                CachedModelBundle {
                    identity,
                    max_model_len,
                    max_logprobs,
                    dtype: dtype_key,
                    prepared_tokenizer: prepared_key,
                    bundle: bundle.clone(),
                },
            );
        runtimes.insert(model, ModelRuntime::new(bundle));
    }
    Ok(runtimes)
}

fn unsupported_media_capabilities(capabilities: &BTreeSet<String>) -> Vec<&str> {
    ["multimodal.video", "multimodal.audio"]
        .into_iter()
        .filter(|capability| capabilities.contains(*capability))
        .collect()
}

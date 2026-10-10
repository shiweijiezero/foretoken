// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Builds complete runtime generations from immutable serving snapshots.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use foretoken_admission::{AdmissionTargetState, PreparedAdmissions};
use foretoken_backend_registry::{
    BackendRegistry, BackendRegistryBuild, ModelIdentity, ServingSnapshot,
};
use foretoken_kv_indexer::{KvIndexDegradedReason, KvIndexer};
use foretoken_llm_facade::LlmFacadeResolver;
use foretoken_router::{
    PipelineRouter, RouteInventory, RouteTargetStatsReader, Router, RouterPipeline,
    RouterPipelineConfigError,
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

struct CachedBackends {
    snapshot: ServingSnapshot,
    registry: Arc<BackendRegistry>,
    kv_indexer: Arc<KvIndexer>,
    models: BTreeMap<String, Arc<CachedModelBundle>>,
    video_models: BTreeSet<String>,
    video_client: Option<reqwest::Client>,
}

pub struct RuntimeBuilder {
    logging: Arc<foretoken_tracing::LogControl>,
    backends: Arc<Mutex<Option<Arc<CachedBackends>>>>,
    router_pipeline: Arc<Mutex<Option<Arc<RouterPipeline>>>>,
    kv_credential: KvIndexCredential,
    routing_load: foretoken_router::RoutingLoadState,
}

impl RuntimeBuilder {
    /// Creates the snapshot builder retained by the frontend watcher for successive updates.
    ///
    /// Successive candidates reuse published algorithm state, load counters and model processors.
    pub fn new(
        kv_credential: KvIndexCredential,
        logging: Arc<foretoken_tracing::LogControl>,
    ) -> Self {
        Self {
            logging,
            backends: Arc::default(),
            router_pipeline: Arc::default(),
            kv_credential,
            routing_load: Default::default(),
        }
    }

    /// Decodes controller-provided bytes into a serving snapshot candidate for [`Self::build`].
    pub fn parse(&self, bytes: &[u8]) -> Result<ServingSnapshot, RuntimeBuildError> {
        Ok(serde_json::from_slice(bytes)?)
    }

    /// Prepares one publishable runtime generation from a decoded snapshot.
    ///
    /// The snapshot watcher calls this before publication. It returns a complete runtime candidate
    /// whose ownership transfers to [`PreparedRuntime::publish`], or an error while the active
    /// runtime and published artifact caches remain unchanged.
    // Validate settings and model contracts, reuse or prepare backend artifacts, then compose
    // the generation. Only publication commits settings and caches to the long-lived owners.
    pub async fn build(
        &self,
        snapshot: ServingSnapshot,
    ) -> Result<PreparedRuntime, RuntimeBuildError> {
        let version = snapshot.version;
        let settings = &snapshot.settings;
        let log_level = self
            .logging
            .prepare(&settings.log_level)
            .map_err(RuntimeBuildError::InvalidSnapshot)?;
        if settings.request_timeout_seconds == 0
            || settings.stream_idle_seconds == 0
            || settings.stream_idle_seconds > settings.request_timeout_seconds
        {
            return Err(RuntimeBuildError::InvalidSnapshot(
                "invalid request timeouts".into(),
            ));
        }
        let request_timeout = std::time::Duration::from_secs(settings.request_timeout_seconds);
        let stream_idle = std::time::Duration::from_secs(settings.stream_idle_seconds);
        let pipeline = Arc::new({
            let published = self
                .router_pipeline
                .lock()
                .expect("router pipeline lock poisoned");
            match published.as_deref() {
                Some(previous) => settings.router_pipeline.rebuild(previous)?,
                None => settings.router_pipeline.build()?,
            }
        });
        let identities = snapshot
            .model_identities()
            .map_err(|error| RuntimeBuildError::InvalidSnapshot(error.to_string()))?;
        if identities.keys().ne(snapshot.admission.keys()) {
            return Err(RuntimeBuildError::InvalidSnapshot(
                "model catalog and admission rules differ".into(),
            ));
        }
        let admission = PreparedAdmissions::new(&snapshot.admission)
            .map_err(|error| RuntimeBuildError::InvalidSnapshot(error.to_string()))?;
        let admission_targets = snapshot
            .admission_target_sets()
            .map_err(|error| RuntimeBuildError::InvalidSnapshot(error.to_string()))?;
        let backends = self.prepare_backends(snapshot, identities).await?;
        let registry = backends.registry.clone();
        let kv_indexer = backends.kv_indexer.clone();
        let control = Arc::new(RegistryRuntimeControl {
            logging: self.logging.clone(),
            log_level,
            backends: backends.clone(),
            published_backends: self.backends.clone(),
            routing_load: self.routing_load.clone(),
            pipeline: pipeline.clone(),
            published_pipeline: self.router_pipeline.clone(),
        });
        let models = backends
            .models
            .iter()
            .map(|(model, cached)| (model.clone(), ModelRuntime::new(cached.bundle.clone())))
            .collect();
        let router: Arc<dyn Router> = Arc::new(
            PipelineRouter::with_pipeline(registry.clone(), pipeline)
                .with_snapshot_version(version)
                .with_load_state(self.routing_load.clone())
                .with_kv_prefix_indexer(kv_indexer)
                .with_route_target_stats_reader(registry.clone()),
        );
        let video_inventory: Arc<dyn foretoken_router::RouteInventory> = registry.clone();
        let resolver: Arc<dyn LlmFacadeResolver> = registry;
        let mut state = RuntimeState::new(models, router, resolver, request_timeout, stream_idle);
        if let Some(client) = &backends.video_client {
            state = state.with_video_backend(
                backends.video_models.clone(),
                video_inventory,
                client.clone(),
            );
        }
        for (model, candidates) in admission_targets {
            state = state.with_admission_targets(model, candidates);
        }
        Ok(PreparedRuntime {
            version,
            state: Arc::new(state),
            control,
            admission,
        })
    }

    /// Reuses published backend artifacts or prepares an independent discovery replacement.
    /// Settings-only updates do not depend on current backend health. New discovery is probed
    /// before and after model loading; failures never mutate the published model-bundle cache.
    async fn prepare_backends(
        &self,
        snapshot: ServingSnapshot,
        identities: BTreeMap<String, ModelIdentity>,
    ) -> Result<Arc<CachedBackends>, RuntimeBuildError> {
        let published = self
            .backends
            .lock()
            .expect("backend cache lock poisoned")
            .clone();
        if let Some(cached) = &published
            && cached.snapshot.same_discovery(&snapshot)
        {
            return Ok(cached.clone());
        }

        let has_physical_backends = !snapshot.groups.is_empty()
            || !snapshot.pd_components.is_empty()
            || !snapshot.epd_components.is_empty();
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
        let (_, _) = tokio::join!(registry.refresh_backend_readiness(), kv_indexer.refresh());
        let healthy_models = registry
            .healthy_models()
            .into_iter()
            .collect::<BTreeSet<_>>();
        // A published discovery cache must contain processors for every routed model;
        // health refresh cannot prepare a missing processor when its backend recovers.
        if registry
            .model_routes()
            .routes()
            .iter()
            .any(|route| !healthy_models.contains(&route.model))
        {
            return Err(RuntimeBuildError::BackendUnavailable);
        }
        let video_models = identities
            .iter()
            .filter(|(model, identity)| {
                healthy_models.contains(*model) && identity.capabilities.contains("video")
            })
            .map(|(model, _)| model.clone())
            .collect::<BTreeSet<_>>();
        let models = if has_physical_backends {
            model_bundles(
                identities
                    .into_iter()
                    .filter(|(model, _)| {
                        healthy_models.contains(model) && !video_models.contains(model)
                    })
                    .collect(),
                &registry,
                published.as_ref().map(|cached| &cached.models),
            )
            .await?
        } else {
            BTreeMap::new()
        };
        let video_client = if video_models.is_empty() {
            None
        } else {
            Some(
                reqwest::Client::builder()
                    .connect_timeout(std::time::Duration::from_secs(10))
                    .build()
                    .map_err(|error| RuntimeBuildError::ModelRuntime(error.to_string()))?,
            )
        };

        // Model preparation can be slow; discovery replacements must still be healthy when sealed.
        let (_, _) = tokio::join!(registry.refresh_backend_readiness(), kv_indexer.refresh());
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
        Ok(Arc::new(CachedBackends {
            snapshot,
            registry,
            kv_indexer,
            models,
            video_models,
            video_client,
        }))
    }
}

pub struct PreparedRuntime {
    version: u64,
    state: Arc<RuntimeState>,
    control: Arc<dyn RuntimeControl>,
    admission: PreparedAdmissions,
}

impl PreparedRuntime {
    /// Atomically hands this prepared candidate to the long-lived serving generation.
    ///
    /// Snapshot watchers consume the candidate exactly once. Returns whether its version replaced
    /// the active state; stale candidates are dropped without affecting request handling.
    pub fn publish(self, generation: &RuntimeGeneration) -> bool {
        generation.replace_state(self.version, self.state, self.control, self.admission)
    }
}

#[derive(Debug, Error)]
pub enum RuntimeBuildError {
    #[error("invalid routing configuration: {0}")]
    Router(#[from] RouterPipelineConfigError),
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
    logging: Arc<foretoken_tracing::LogControl>,
    log_level: tracing::level_filters::LevelFilter,
    pipeline: Arc<RouterPipeline>,
    published_pipeline: Arc<Mutex<Option<Arc<RouterPipeline>>>>,
    backends: Arc<CachedBackends>,
    published_backends: Arc<Mutex<Option<Arc<CachedBackends>>>>,
    routing_load: foretoken_router::RoutingLoadState,
}

#[async_trait]
impl RuntimeControl for RegistryRuntimeControl {
    fn activate(&self, version: u64) {
        self.pipeline.activate(version);
        self.logging.apply(self.log_level);
        *self
            .published_backends
            .lock()
            .expect("backend cache lock poisoned") = Some(self.backends.clone());
        *self
            .published_pipeline
            .lock()
            .expect("router pipeline lock poisoned") = Some(self.pipeline.clone());
    }

    async fn refresh_backend_readiness(&self) {
        let (_, _) = tokio::join!(
            self.backends.registry.refresh_backend_readiness(),
            self.backends.kv_indexer.refresh(),
        );
    }

    fn configured_models(&self) -> Vec<String> {
        self.backends.registry.configured_models()
    }

    fn is_ready(&self) -> bool {
        self.backends.registry.is_configured()
    }

    fn model_ready(&self, model: &str) -> bool {
        self.backends.registry.is_model_ready(model)
    }

    fn route_target_states(
        &self,
        model: &str,
        window: std::time::Duration,
    ) -> Vec<AdmissionTargetState> {
        self.backends
            .registry
            .model_routes()
            .routes()
            .iter()
            .filter(|target| target.model == model)
            .map(|target| {
                let mut target = target.clone();
                target.capabilities = self
                    .backends
                    .registry
                    .effective_capabilities(&target.route_target_id);
                AdmissionTargetState {
                    healthy: self
                        .backends
                        .registry
                        .is_route_target_healthy(&target.route_target_id),
                    statistics: self
                        .backends
                        .registry
                        .stats(&target.route_target_id, window),
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
        let status = self.backends.kv_indexer.status();
        KvIndexDiagnostics {
            state: status.state.as_str().into(),
            reason: status.reason.map(|reason| reason.as_str().into()),
            sources_healthy: status.sources_healthy,
            sources_total: status.sources_total,
        }
    }
}

/// Prepares model processors from the new registry, reusing matching published bundles read-only.
/// The returned map belongs to the candidate backend cache until successful runtime publication.
async fn model_bundles(
    identities: BTreeMap<String, ModelIdentity>,
    registry: &BackendRegistry,
    cached_bundles: Option<&BTreeMap<String, Arc<CachedModelBundle>>>,
) -> Result<BTreeMap<String, Arc<CachedModelBundle>>, RuntimeBuildError> {
    let mut bundles = BTreeMap::new();
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
        if let Some(cached) = cached_bundles
            .and_then(|bundles| bundles.get(&model))
            .filter(|cached| {
                cached.identity == identity
                    && cached.max_model_len == max_model_len
                    && cached.max_logprobs == max_logprobs
                    && cached.dtype == dtype_key
                    && cached.prepared_tokenizer == prepared_key
            })
        {
            bundles.insert(model, cached.clone());
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
        bundles.insert(
            model,
            Arc::new(CachedModelBundle {
                identity,
                max_model_len,
                max_logprobs,
                dtype: dtype_key,
                prepared_tokenizer: prepared_key,
                bundle,
            }),
        );
    }
    Ok(bundles)
}

fn unsupported_media_capabilities(capabilities: &BTreeSet<String>) -> Vec<&str> {
    ["multimodal.video", "multimodal.audio"]
        .into_iter()
        .filter(|capability| capabilities.contains(*capability))
        .collect()
}

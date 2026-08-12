// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Builds complete runtime generations from immutable serving snapshots.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use async_trait::async_trait;
use foretoken_backend_registry::{
    BackendRegistry, BackendRegistryBuild, ModelIdentity, ServingSnapshot,
};
use foretoken_kv_indexer::{KvIndexDegradedReason, KvIndexer};
use foretoken_llm_facade::LlmFacadeResolver;
use foretoken_router::{PipelineRouter, RouteTargetSet, Router, RouterPipelineConfig};
use foretoken_server::{
    KvIndexDiagnostics, ModelRuntime, RuntimeBundle, RuntimeControl, RuntimeGeneration,
    RuntimeState,
};
use foretoken_text::{HfSnapshotRuntime, load_hf_snapshot_runtime};
use thiserror::Error;

#[derive(Debug, Clone, Copy)]
pub enum KvIndexCredential {
    Disabled,
    Key([u8; 32]),
    Degraded(KvIndexDegradedReason),
}

pub struct RuntimeBuilder {
    router_pipeline: RouterPipelineConfig,
    kv_credential: KvIndexCredential,
}

impl RuntimeBuilder {
    pub fn new(router_pipeline: RouterPipelineConfig, kv_credential: KvIndexCredential) -> Self {
        Self {
            router_pipeline,
            kv_credential,
        }
    }

    pub fn parse(&self, bytes: &[u8]) -> Result<ServingSnapshot, RuntimeBuildError> {
        Ok(serde_json::from_slice(bytes)?)
    }

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
        let logical_targets = snapshot
            .logical_target_sets()
            .map_err(|error| RuntimeBuildError::InvalidSnapshot(error.to_string()))?
            .into_iter()
            .filter_map(|(model, targets)| targets.into_iter().next().map(|target| (model, target)))
            .collect::<BTreeMap<String, RouteTargetSet>>();
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
        });
        control.refresh_backend_readiness().await;
        if has_physical_backends && !registry.is_ready() {
            return Err(RuntimeBuildError::BackendUnavailable);
        }
        let models = if has_physical_backends {
            model_runtimes(identities, &registry).await?
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
            if !models.keys().all(|model| healthy_models.contains(model)) {
                return Err(RuntimeBuildError::BackendBecameUnavailable);
            }
        }
        let router: Arc<dyn Router> = Arc::new(
            PipelineRouter::with_pipeline(registry.clone(), self.router_pipeline.build())
                .with_kv_prefix_indexer(kv_indexer)
                .with_route_target_stats_reader(registry.clone()),
        );
        let resolver: Arc<dyn LlmFacadeResolver> = registry;
        Ok(PreparedRuntime {
            version,
            state: Arc::new(
                RuntimeState::new(models, router, resolver).with_logical_targets(logical_targets),
            ),
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
    pub fn version(&self) -> u64 {
        self.version
    }

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
}

#[async_trait]
impl RuntimeControl for RegistryRuntimeControl {
    async fn refresh_backend_readiness(&self) {
        let (_, _) = tokio::join!(
            self.registry.refresh_backend_readiness(),
            self.kv_indexer.refresh(),
        );
    }

    fn healthy_models(&self) -> Vec<String> {
        self.registry.configured_models()
    }

    fn is_ready(&self) -> bool {
        self.registry.is_configured()
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
) -> Result<BTreeMap<String, ModelRuntime>, RuntimeBuildError> {
    let mut runtimes = BTreeMap::new();
    for (model, identity) in identities {
        let max_model_len = registry.effective_max_model_len(&model).ok_or_else(|| {
            RuntimeBuildError::ModelRuntime(format!(
                "no healthy runtime metadata for model {model}"
            ))
        })?;
        let model_dtype = registry.effective_model_dtype(&model).ok_or_else(|| {
            RuntimeBuildError::ModelRuntime(format!(
                "no consistent runtime dtype for model {model}"
            ))
        })?;
        let HfSnapshotRuntime {
            text_processor,
            tokenizer,
            chat_processor,
            supports_multimodal,
        } = load_hf_snapshot_runtime(
            &identity.tokenizer,
            &identity.tokenizer_revision,
            max_model_len,
            model_dtype,
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
        runtimes.insert(
            model,
            ModelRuntime::new(
                identity.revision,
                Arc::new(RuntimeBundle::new(
                    text_processor,
                    tokenizer,
                    chat_processor,
                )),
            ),
        );
    }
    Ok(runtimes)
}

fn unsupported_media_capabilities(capabilities: &BTreeSet<String>) -> Vec<&str> {
    ["multimodal.video", "multimodal.audio"]
        .into_iter()
        .filter(|capability| capabilities.contains(*capability))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn builder() -> RuntimeBuilder {
        RuntimeBuilder::new(RouterPipelineConfig::default(), KvIndexCredential::Disabled)
    }

    #[test]
    fn snapshot_is_parsed_without_building_model_runtime() {
        assert_eq!(
            builder()
                .parse(br#"{"version":42,"groups":[]}"#)
                .unwrap()
                .version,
            42
        );
        assert!(builder().parse(b"not-json").is_err());
    }

    #[tokio::test]
    async fn logical_only_snapshot_publishes_a_ready_scale_from_zero_runtime() {
        let snapshot = ServingSnapshot {
            version: 1,
            models: vec![foretoken_backend_registry::SnapshotModel {
                service_uid: "service".into(),
                model: "model".into(),
                revision: "r1".into(),
                tokenizer: "tokenizer".into(),
                tokenizer_revision: "r1".into(),
                capabilities: ["chat".into()].into_iter().collect(),
                max_input_tokens: Some(2048),
                targets: vec![foretoken_router::ScalingTarget {
                    service_uid: "service".into(),
                    name: "default".into(),
                    uid: "pool".into(),
                    kind: foretoken_router::ScalingTargetKind::Pool,
                }],
            }],
            groups: vec![],
            pd_components: vec![],
            pd_domains: vec![],
            epd_components: vec![],
            epd_domains: vec![],
        };
        let prepared = builder().build(snapshot).await.unwrap();
        let generation = RuntimeGeneration::new();

        assert!(prepared.publish(&generation));
        assert!(foretoken_server::Generation::ready(&generation));
        assert_eq!(generation.models(), vec!["model"]);
    }

    #[tokio::test]
    async fn invalid_snapshot_fails_before_runtime_preparation() {
        let conflicting = br#"{"version":1,"groups":[
            {"route_target_id":"a","model":"model","revision":"r1","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://a","data_parallel_size":1},
            {"route_target_id":"b","model":"model","revision":"r2","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://b","data_parallel_size":1}
        ]}"#;
        let builder = builder();
        let snapshot = builder.parse(conflicting).unwrap();
        assert!(matches!(
            builder.build(snapshot).await,
            Err(RuntimeBuildError::InvalidSnapshot(_))
        ));
    }
}

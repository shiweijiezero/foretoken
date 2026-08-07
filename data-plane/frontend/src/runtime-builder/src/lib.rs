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
use foretoken_router::{PolicyRouter, Router, RouterAlgorithm};
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
    router_algorithm: RouterAlgorithm,
    kv_credential: KvIndexCredential,
}

impl RuntimeBuilder {
    pub fn new(router_algorithm: RouterAlgorithm, kv_credential: KvIndexCredential) -> Self {
        Self {
            router_algorithm,
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
            KvIndexCredential::Disabled => KvIndexer::new(kv_runtime_config, None),
            KvIndexCredential::Key(key) => KvIndexer::new(kv_runtime_config, Some(key)),
            KvIndexCredential::Degraded(reason) => KvIndexer::degraded(kv_runtime_config, reason),
        });
        let control = Arc::new(RegistryRuntimeControl {
            registry: registry.clone(),
            kv_indexer: kv_indexer.clone(),
        });
        control.refresh_backend_readiness().await;
        if !registry.is_ready() {
            return Err(RuntimeBuildError::BackendUnavailable);
        }
        let models = model_runtimes(
            snapshot
                .model_identities()
                .map_err(|error| RuntimeBuildError::InvalidSnapshot(error.to_string()))?,
            &registry,
        )
        .await?;

        // Preparation may be slow. Probe again so only a fully ready candidate can be published.
        control.refresh_backend_readiness().await;
        let healthy_models = registry
            .healthy_models()
            .into_iter()
            .collect::<BTreeSet<_>>();
        if !models.keys().all(|model| healthy_models.contains(model)) {
            return Err(RuntimeBuildError::BackendBecameUnavailable);
        }
        let router: Arc<dyn Router> = Arc::new(PolicyRouter::with_algorithm(
            registry.clone(),
            kv_indexer,
            self.router_algorithm,
        ));
        let resolver: Arc<dyn LlmFacadeResolver> = registry;
        Ok(PreparedRuntime {
            version,
            state: Arc::new(RuntimeState::new(models, router, resolver)),
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
        self.registry.healthy_models()
    }

    fn is_ready(&self) -> bool {
        self.registry.is_ready()
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
        if identity
            .capabilities
            .iter()
            .any(|capability| capability == "multimodal" || capability.starts_with("multimodal."))
            && !supports_multimodal
        {
            return Err(RuntimeBuildError::ModelRuntime(format!(
                "model {model} declares multimodal routing capabilities but its frontend processor does not support multimodal input"
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

#[cfg(test)]
mod tests {
    use super::*;

    fn builder() -> RuntimeBuilder {
        RuntimeBuilder::new(RouterAlgorithm::KvAware, KvIndexCredential::Disabled)
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
    async fn invalid_snapshot_fails_before_runtime_preparation() {
        let conflicting = br#"{"version":1,"groups":[
            {"backend_id":"a","model":"model","revision":"r1","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://a"},
            {"backend_id":"b","model":"model","revision":"r2","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://b"}
        ]}"#;
        let builder = builder();
        let snapshot = builder.parse(conflicting).unwrap();
        assert!(matches!(
            builder.build(snapshot).await,
            Err(RuntimeBuildError::InvalidSnapshot(_))
        ));
    }
}

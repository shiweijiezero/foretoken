// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Resolves model and execution-role overrides and retains their independently owned algorithms.

use std::collections::BTreeMap;
use std::sync::Arc;

use foretoken_model_protocol::ModelServerRole;
use serde::{Deserialize, Serialize};

use super::{
    FilterStage, PickerStage, RouterPipeline, RouterPipelineConfig, RouterPipelineConfigError,
    ScorerStage,
};

/// Optional algorithm selections; each present algorithm and its parameters replace the inherited slot.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RouterPipelineOverride {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub filter: Option<FilterStage>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scorer: Option<ScorerStage>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub picker: Option<PickerStage>,
}

/// ModelService routing choices, inherited from the frontend and specialized by execution role.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelRouterPipeline {
    #[serde(flatten)]
    pub pipeline: RouterPipelineOverride,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub encoder: Option<RouterPipelineOverride>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prefill: Option<RouterPipelineOverride>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decode: Option<RouterPipelineOverride>,
}

/// Model execution roles shared by resolved configuration and compiled routing pipelines.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelRoutingStages<T> {
    pub aggregate: T,
    pub encoder: T,
    pub prefill: T,
    pub decode: T,
}

/// Immutable algorithms for one runtime generation, isolated by model and execution role.
/// The runtime publisher activates shared state only after accepting the complete generation.
pub struct RouterPipelines<C: Send + 'static = ()> {
    default: Arc<RouterPipeline<C>>,
    models: BTreeMap<String, ModelRoutingStages<Arc<RouterPipeline<C>>>>,
}

impl ModelRouterPipeline {
    /// Resolves this model's execution-role choices against the frontend defaults.
    pub fn resolve(
        &self,
        inherited: &RouterPipelineConfig,
    ) -> ModelRoutingStages<RouterPipelineConfig> {
        let model = self.pipeline.resolve(inherited);
        ModelRoutingStages {
            encoder: self
                .encoder
                .as_ref()
                .map_or_else(|| model.clone(), |stage| stage.resolve(&model)),
            prefill: self
                .prefill
                .as_ref()
                .map_or_else(|| model.clone(), |stage| stage.resolve(&model)),
            decode: self
                .decode
                .as_ref()
                .map_or_else(|| model.clone(), |stage| stage.resolve(&model)),
            aggregate: model,
        }
    }
}

impl RouterPipelines {
    /// Builds a complete model/role generation, reusing history only from the same model and role.
    /// Callers supply resolved configs and retain the previous published set if construction fails.
    pub fn build(
        default: &RouterPipelineConfig,
        models: BTreeMap<String, ModelRoutingStages<RouterPipelineConfig>>,
        previous: Option<&Self>,
    ) -> Result<Self, RouterPipelineConfigError> {
        let fallback = match previous {
            Some(previous) => default.rebuild(&previous.default)?,
            None => default.build()?,
        };
        let mut compiled = BTreeMap::new();
        for (model, configs) in models {
            let old = previous.and_then(|previous| previous.models.get(&model));
            let build = |role, config: RouterPipelineConfig| {
                let pipeline = match old {
                    Some(stages) => config.rebuild(stages.get(role)),
                    None => config.build(),
                }
                .map_err(|source| RouterPipelineConfigError::ModelStage {
                    model: model.clone(),
                    stage: match role {
                        ModelServerRole::Aggregate => "aggregate",
                        ModelServerRole::Encoder => "encoder",
                        ModelServerRole::Prefill => "prefill",
                        ModelServerRole::Decode => "decode",
                    },
                    source: Box::new(source),
                })?;
                Ok::<_, RouterPipelineConfigError>(Arc::new(pipeline))
            };
            let stages = ModelRoutingStages {
                aggregate: build(ModelServerRole::Aggregate, configs.aggregate)?,
                encoder: build(ModelServerRole::Encoder, configs.encoder)?,
                prefill: build(ModelServerRole::Prefill, configs.prefill)?,
                decode: build(ModelServerRole::Decode, configs.decode)?,
            };
            compiled.insert(model, stages);
        }
        Ok(Self {
            default: Arc::new(fallback),
            models: compiled,
        })
    }
}

impl<C: Send + 'static> RouterPipelines<C> {
    /// Wraps a caller-provided pipeline for all models and roles without changing its custom context.
    pub fn new(pipeline: impl Into<Arc<RouterPipeline<C>>>) -> Self {
        Self {
            default: pipeline.into(),
            models: BTreeMap::new(),
        }
    }

    /// Reports whether a model role's scorer consumes daily request-length observations.
    pub fn needs_request_length_history(&self, model: &str, role: ModelServerRole) -> bool {
        self.get(model, role).scorer.needs_request_length_history()
    }

    /// Commits scorer settings at the existing atomic runtime-publication boundary.
    pub fn activate(&self, version: u64) {
        self.default.activate(version);
        for stages in self.models.values() {
            for pipeline in [
                &stages.aggregate,
                &stages.encoder,
                &stages.prefill,
                &stages.decode,
            ] {
                pipeline.activate(version);
            }
        }
    }

    /// Returns the resolved algorithms for one model's execution role.
    pub(crate) fn get(&self, model: &str, role: ModelServerRole) -> &Arc<RouterPipeline<C>> {
        self.models
            .get(model)
            .map_or(&self.default, |stages| stages.get(role))
    }
}

impl RouterPipelineOverride {
    /// Resolves complete algorithm slots without merging parameters across algorithms.
    fn resolve(&self, inherited: &RouterPipelineConfig) -> RouterPipelineConfig {
        RouterPipelineConfig {
            filter: self
                .filter
                .clone()
                .unwrap_or_else(|| inherited.filter.clone()),
            scorer: self
                .scorer
                .clone()
                .unwrap_or_else(|| inherited.scorer.clone()),
            picker: self
                .picker
                .clone()
                .unwrap_or_else(|| inherited.picker.clone()),
        }
    }
}

impl<T> ModelRoutingStages<T> {
    /// Selects the value owned by one execution role without changing its lifetime.
    fn get(&self, role: ModelServerRole) -> &T {
        match role {
            ModelServerRole::Aggregate => &self.aggregate,
            ModelServerRole::Encoder => &self.encoder,
            ModelServerRole::Prefill => &self.prefill,
            ModelServerRole::Decode => &self.decode,
        }
    }
}

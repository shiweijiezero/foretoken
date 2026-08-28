// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Defines the controller-projected component/pipeline-scope routing snapshot contract.
use foretoken_model_protocol::ModelServerRole;
use foretoken_router::{RouteTargetId, RouteTargetSet, ScalingTarget, ScalingTargetKind};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServingSnapshot {
    pub version: u64,
    #[serde(default)]
    pub models: Vec<SnapshotModel>,
    pub groups: Vec<SnapshotGroup>,
    #[serde(default)]
    pub pd_components: Vec<SnapshotPdComponent>,
    #[serde(default)]
    pub pd_pipeline_scopes: Vec<SnapshotPdPipelineScope>,
    #[serde(default)]
    pub epd_components: Vec<SnapshotEpdComponent>,
    #[serde(default)]
    pub epd_pipeline_scopes: Vec<SnapshotEpdPipelineScope>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotModel {
    pub service_uid: String,
    pub model: String,
    pub revision: String,
    pub tokenizer: String,
    pub tokenizer_revision: String,
    #[serde(default)]
    pub capabilities: BTreeSet<String>,
    pub admission_target_sets: Vec<RouteTargetSet>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotEpdComponent {
    #[serde(default)]
    pub service_uid: String,
    #[serde(default)]
    pub pool_uid: String,
    #[serde(default)]
    pub pool_name: String,
    pub route_target_id: RouteTargetId,
    pub role: ModelServerRole,
    pub pipeline_scope_id: String,
    pub model: String,
    pub revision: String,
    pub tokenizer: String,
    pub tokenizer_revision: String,
    #[serde(default)]
    pub profile_name: String,
    #[serde(default)]
    pub profile_revision: String,
    #[serde(default)]
    pub connector: String,
    #[serde(default)]
    pub protocol: String,
    #[serde(default)]
    pub ec_profile_name: String,
    #[serde(default)]
    pub ec_profile_revision: String,
    #[serde(default)]
    pub ec_connector: String,
    #[serde(default)]
    pub capabilities: BTreeSet<String>,
    #[serde(default)]
    pub max_input_tokens: Option<usize>,
    pub endpoint: String,
    #[serde(default)]
    pub prefill_bootstrap_endpoint: Option<String>,
    #[serde(default)]
    pub kv_scope_id: String,
    pub data_parallel_size: u32,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotEpdPipelineScope {
    pub pipeline_scope_id: String,
    pub encoder_route_target_id: RouteTargetId,
    pub prefill_route_target_id: RouteTargetId,
    pub decode_route_target_id: RouteTargetId,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotPdComponent {
    #[serde(default)]
    pub service_uid: String,
    #[serde(default)]
    pub pool_uid: String,
    #[serde(default)]
    pub pool_name: String,
    pub route_target_id: RouteTargetId,
    pub role: ModelServerRole,
    pub pipeline_scope_id: String,
    pub model: String,
    pub revision: String,
    pub tokenizer: String,
    pub tokenizer_revision: String,
    pub profile_name: String,
    pub profile_revision: String,
    pub connector: String,
    pub protocol: String,
    #[serde(default)]
    pub capabilities: BTreeSet<String>,
    #[serde(default)]
    pub max_input_tokens: Option<usize>,
    pub endpoint: String,
    #[serde(default)]
    pub prefill_bootstrap_endpoint: Option<String>,
    #[serde(default)]
    pub kv_scope_id: String,
    pub data_parallel_size: u32,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotPdPipelineScope {
    pub pipeline_scope_id: String,
    pub prefill_route_target_ids: Vec<RouteTargetId>,
    pub decode_route_target_ids: Vec<RouteTargetId>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotGroup {
    #[serde(default)]
    pub service_uid: String,
    #[serde(default)]
    pub pool_uid: String,
    #[serde(default)]
    pub pool_name: String,
    pub route_target_id: RouteTargetId,
    pub model: String,
    pub revision: String,
    pub tokenizer: String,
    pub tokenizer_revision: String,
    #[serde(default)]
    pub capabilities: BTreeSet<String>,
    #[serde(default)]
    pub max_input_tokens: Option<usize>,
    pub endpoint: String,
    #[serde(default)]
    pub kv_scope_id: String,
    pub data_parallel_size: u32,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelIdentity {
    pub revision: String,
    pub tokenizer: String,
    pub tokenizer_revision: String,
    pub capabilities: BTreeSet<String>,
}
impl ServingSnapshot {
    // Prefer the controller-projected logical targets in `models`. Model-less snapshots fall
    // back to topology-derived targets, then all sets are ordered and deduplicated consistently.
    pub fn admission_target_sets(
        &self,
    ) -> Result<BTreeMap<String, Vec<RouteTargetSet>>, SnapshotError> {
        let mut targets = BTreeMap::<String, Vec<RouteTargetSet>>::new();
        for model in &self.models {
            if model.service_uid.is_empty()
                || model.model.is_empty()
                || model.admission_target_sets.is_empty()
                || model.admission_target_sets.iter().any(|targets| {
                    targets.targets().is_empty()
                        || targets.targets().iter().any(|target| {
                            target.service_uid != model.service_uid
                                || target.name.is_empty()
                                || target.uid.is_empty()
                        })
                })
            {
                return Err(SnapshotError::IncompleteScalingModel(model.model.clone()));
            }
            targets
                .entry(model.model.clone())
                .or_default()
                .extend(model.admission_target_sets.clone());
        }
        if targets.is_empty() {
            for group in &self.groups {
                targets
                    .entry(group.model.clone())
                    .or_default()
                    .push(RouteTargetSet::new(vec![ScalingTarget {
                        service_uid: group.service_uid.clone(),
                        name: group.pool_name.clone(),
                        uid: group.pool_uid.clone(),
                        kind: ScalingTargetKind::Pool,
                    }]));
            }
            let mut pd_targets = BTreeMap::<(String, String), Vec<ScalingTarget>>::new();
            for component in &self.pd_components {
                pd_targets
                    .entry((component.model.clone(), component.service_uid.clone()))
                    .or_default()
                    .push(ScalingTarget {
                        service_uid: component.service_uid.clone(),
                        name: component.pool_name.clone(),
                        uid: component.pool_uid.clone(),
                        kind: ScalingTargetKind::Pool,
                    });
            }
            for ((model, _), values) in pd_targets {
                targets
                    .entry(model)
                    .or_default()
                    .push(RouteTargetSet::new(values));
            }
            for component in &self.epd_components {
                targets
                    .entry(component.model.clone())
                    .or_default()
                    .push(RouteTargetSet::new(vec![ScalingTarget {
                        service_uid: component.service_uid.clone(),
                        name: "epd".into(),
                        uid: component.service_uid.clone(),
                        kind: ScalingTargetKind::EPDPipelineScope,
                    }]));
            }
        }
        for values in targets.values_mut() {
            values.sort_by(|left, right| left.targets().cmp(right.targets()));
            values.dedup();
        }
        Ok(targets)
    }

    pub fn model_identities(&self) -> Result<BTreeMap<String, ModelIdentity>, SnapshotError> {
        let mut identities = BTreeMap::new();
        for (model, revision, tokenizer, tokenizer_revision, capabilities) in self
            .models
            .iter()
            .map(|model| {
                (
                    &model.model,
                    &model.revision,
                    &model.tokenizer,
                    &model.tokenizer_revision,
                    &model.capabilities,
                )
            })
            .chain(self.groups.iter().map(|g| {
                (
                    &g.model,
                    &g.revision,
                    &g.tokenizer,
                    &g.tokenizer_revision,
                    &g.capabilities,
                )
            }))
            .chain(self.pd_components.iter().map(|c| {
                (
                    &c.model,
                    &c.revision,
                    &c.tokenizer,
                    &c.tokenizer_revision,
                    &c.capabilities,
                )
            }))
            .chain(self.epd_components.iter().map(|c| {
                (
                    &c.model,
                    &c.revision,
                    &c.tokenizer,
                    &c.tokenizer_revision,
                    &c.capabilities,
                )
            }))
        {
            if model.is_empty()
                || revision.is_empty()
                || tokenizer.is_empty()
                || tokenizer_revision.is_empty()
            {
                return Err(SnapshotError::IncompleteModelIdentity);
            }
            let value = ModelIdentity {
                revision: revision.clone(),
                tokenizer: tokenizer.clone(),
                tokenizer_revision: tokenizer_revision.clone(),
                capabilities: capabilities.clone(),
            };
            match identities.get(model) {
                Some(existing) if existing != &value => {
                    return Err(SnapshotError::ConflictingIdentity(model.clone()));
                }
                Some(_) => {}
                None => {
                    identities.insert(model.clone(), value);
                }
            }
        }
        Ok(identities)
    }
}
#[derive(Debug, Error)]
pub enum SnapshotError {
    #[error("routing snapshot version must be greater than zero")]
    InvalidVersion,
    #[error("routing snapshot has an incomplete model or tokenizer identity")]
    IncompleteModelIdentity,
    #[error("routing snapshot has an incomplete logical scaling model {0:?}")]
    IncompleteScalingModel(String),
    #[error("routing snapshot has no admission target set for scaling target {0:?}")]
    MissingAdmissionTarget(String),
    #[error("routing snapshot assigns conflicting admission target sets to scaling target {0:?}")]
    ConflictingAdmissionTarget(String),
    #[error("routing snapshot has incomplete group {0:?}")]
    IncompleteGroup(RouteTargetId),
    #[error("routing snapshot repeats backend {0:?}")]
    DuplicateRouteTarget(RouteTargetId),
    #[error("routing snapshot has incomplete P/D component {0:?}")]
    IncompletePdComponent(RouteTargetId),
    #[error("routing snapshot P/D component {0:?} must use MooncakeConnector over rdma")]
    UnsupportedPdTransport(RouteTargetId),
    #[error(
        "routing configuration P/D linked processing unit {0:?} is incomplete or crosses a ModelService boundary"
    )]
    InvalidPdPipelineScope(String),
    #[error("routing snapshot model {0:?} cannot mix aggregate and P/D routes")]
    MixedModelRoles(String),
    #[error("routing snapshot model {0:?} has conflicting revision or tokenizer identity")]
    ConflictingIdentity(String),
    #[error("routing snapshot endpoint {endpoint:?} is invalid: {message}")]
    InvalidEndpoint { endpoint: String, message: String },
    #[error("routing snapshot has incomplete E/P/D component {0:?}")]
    IncompleteEpdComponent(RouteTargetId),
    #[error(
        "routing configuration E/P/D linked processing unit {0:?} is incomplete, inconsistent, or not a static triplet"
    )]
    InvalidEpdPipelineScope(String),
}

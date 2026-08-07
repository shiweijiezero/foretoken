// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Defines the controller-projected component/domain routing snapshot contract.
use foretoken_router::{BackendId, BackendRole};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServingSnapshot {
    pub version: u64,
    pub groups: Vec<SnapshotGroup>,
    #[serde(default)]
    pub pd_components: Vec<SnapshotPdComponent>,
    #[serde(default)]
    pub pd_domains: Vec<SnapshotPdDomain>,
    #[serde(default)]
    pub epd_components: Vec<SnapshotEpdComponent>,
    #[serde(default)]
    pub epd_domains: Vec<SnapshotEpdDomain>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotEpdComponent {
    pub backend_id: BackendId,
    pub role: BackendRole,
    pub domain_id: String,
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
    pub ec_runtime_fingerprint: String,
    #[serde(default)]
    pub capabilities: BTreeSet<String>,
    #[serde(default)]
    pub max_input_tokens: Option<usize>,
    pub endpoint: String,
    #[serde(default)]
    pub prefill_bootstrap_endpoint: Option<String>,
    #[serde(default)]
    pub kv_scope_id: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotEpdDomain {
    pub domain_id: String,
    pub encoder_backend_id: BackendId,
    pub prefill_backend_id: BackendId,
    pub decode_backend_id: BackendId,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotPdComponent {
    pub backend_id: BackendId,
    pub role: BackendRole,
    pub domain_id: String,
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
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotPdDomain {
    pub domain_id: String,
    pub prefill_backend_ids: Vec<BackendId>,
    pub decode_backend_ids: Vec<BackendId>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotGroup {
    pub backend_id: BackendId,
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
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelIdentity {
    pub revision: String,
    pub tokenizer: String,
    pub tokenizer_revision: String,
    pub capabilities: BTreeSet<String>,
}
impl ServingSnapshot {
    pub fn model_identities(&self) -> Result<BTreeMap<String, ModelIdentity>, SnapshotError> {
        let mut identities = BTreeMap::new();
        for (model, revision, tokenizer, tokenizer_revision, capabilities) in self
            .groups
            .iter()
            .map(|g| {
                (
                    &g.model,
                    &g.revision,
                    &g.tokenizer,
                    &g.tokenizer_revision,
                    &g.capabilities,
                )
            })
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
        if identities.is_empty() {
            Err(SnapshotError::EmptyGroups)
        } else {
            Ok(identities)
        }
    }
}
#[derive(Debug, Error)]
pub enum SnapshotError {
    #[error("routing snapshot is not valid JSON: {0}")]
    Parse(serde_json::Error),
    #[error("routing snapshot version must be greater than zero")]
    InvalidVersion,
    #[error("routing snapshot has no complete groups")]
    EmptyGroups,
    #[error("routing snapshot has an incomplete model or tokenizer identity")]
    IncompleteModelIdentity,
    #[error("routing snapshot has incomplete group {0:?}")]
    IncompleteGroup(BackendId),
    #[error("routing snapshot repeats backend {0:?}")]
    DuplicateBackend(BackendId),
    #[error("routing snapshot has incomplete P/D component {0:?}")]
    IncompletePdComponent(BackendId),
    #[error("routing snapshot P/D component {0:?} must use MooncakeConnector over rdma")]
    UnsupportedPdTransport(BackendId),
    #[error("routing snapshot domain {0:?} is incomplete or crosses a ModelService boundary")]
    InvalidPdDomain(String),
    #[error("routing snapshot model {0:?} cannot mix aggregate and P/D routes")]
    MixedModelRoles(String),
    #[error("routing snapshot model {0:?} has conflicting revision or tokenizer identity")]
    ConflictingIdentity(String),
    #[error("routing snapshot endpoint {endpoint:?} is invalid: {message}")]
    InvalidEndpoint { endpoint: String, message: String },
    #[error("routing snapshot component {0:?} has conflicting KV index endpoint or scope")]
    ConflictingKvEventSource(String),
    #[error("routing snapshot has incomplete E/P/D component {0:?}")]
    IncompleteEpdComponent(BackendId),
    #[error("routing snapshot E/P/D component {0:?} has an invalid EC or KV transfer contract")]
    InvalidEpdTransferContract(BackendId),
    #[error(
        "routing snapshot E/P/D domain {0:?} is incomplete, inconsistent, or not a static triplet"
    )]
    InvalidEpdDomain(String),
}

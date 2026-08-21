// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Route target identities, scaling targets, and static route records.

use std::collections::BTreeSet;

use foretoken_model_protocol::ModelServerRole;
use serde::{Deserialize, Serialize};

/// Stable identity of one routable ModelGroup.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct RouteTargetId(String);
impl RouteTargetId {
    /// Creates a stable route target identity from its serialized value.
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    /// Returns the serialized route target identity.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// Control-plane resource kind used for autoscaling attribution.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "PascalCase")]
pub enum ScalingTargetKind {
    /// ModelPool-owned capacity.
    Pool,
    /// Capacity owned by an E/P/D route set.
    EPDPipelineScope,
}

/// Stable control-plane target that owns route target capacity.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ScalingTarget {
    /// UID of the ModelService that exposes the route.
    pub service_uid: String,
    /// Name of the owning scaling resource.
    pub name: String,
    /// UID of the owning scaling resource.
    pub uid: String,
    /// Kind of the owning scaling resource.
    pub kind: ScalingTargetKind,
}

/// Sorted, deduplicated scaling targets affected by one request.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct RouteTargetSet(Vec<ScalingTarget>);
impl RouteTargetSet {
    /// Creates a deterministically ordered set of scaling targets.
    pub fn new(mut targets: Vec<ScalingTarget>) -> Self {
        targets.sort();
        targets.dedup();
        Self(targets)
    }

    /// Returns the affected scaling targets in deterministic order.
    pub fn targets(&self) -> &[ScalingTarget] {
        &self.0
    }
}

/// Static route record projected for one routable ModelGroup.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouteTarget {
    /// Stable identity of the routable ModelGroup.
    pub route_target_id: RouteTargetId,
    /// Control-plane target that owns the route target's capacity.
    pub target: ScalingTarget,
    /// Complete capacity set that must admit a request using this route.
    pub admission_targets: RouteTargetSet,
    /// Logical model served by the route target.
    pub model: String,
    /// Exact model revision served by the route target.
    pub revision: String,
    /// Request features currently supported by the route target.
    #[serde(default)]
    pub capabilities: BTreeSet<String>,
    /// Maximum accepted prompt length, or no advertised limit.
    pub max_input_tokens: Option<usize>,
    /// Whether the static route is ready for request matching.
    pub ready: bool,
    /// Execution role provided by the route target.
    #[serde(default)]
    pub role: ModelServerRole,
    /// Optional identity for the Encoder, Prefill, and Decode routes in one E/P/D route set.
    #[serde(default)]
    pub pipeline_scope_id: Option<String>,
    /// Number of data-parallel replicas selectable for this route target.
    pub data_parallel_size: u32,
}

/// Route target identity selected for one execution stage.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteDecision {
    /// Stable identity of the selected routable ModelGroup.
    pub route_target_id: RouteTargetId,
    /// Complete capacity set attributed to this request admission.
    pub admission_targets: RouteTargetSet,
    /// Aggregate, Encoder, Prefill, or Decode stage selected for execution.
    pub role: ModelServerRole,
    /// Logical model selected for execution.
    pub model: String,
    /// Exact model revision selected for execution.
    pub revision: String,
    /// Exact data-parallel replica selected for this execution stage; single-rank targets use zero.
    pub data_parallel_rank: u32,
}

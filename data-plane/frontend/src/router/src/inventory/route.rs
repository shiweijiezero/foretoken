// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Destination selected for one request execution stage.

use crate::{RouteTargetId, RouteTargetSet};
use foretoken_model_protocol::ModelServerRole;

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

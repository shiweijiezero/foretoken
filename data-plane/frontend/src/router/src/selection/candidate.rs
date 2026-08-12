// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! One routable ModelGroup candidate and its current routing load.

use foretoken_model_protocol::ModelServerRole;

use crate::{RouteTargetId, ScalingTarget};

/// Current load attached to a candidate snapshot.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct RouteTargetLoad {
    /// Requests currently executing on the route target, or `None` when unavailable.
    pub running_requests: Option<u64>,
}

/// One selectable routable ModelGroup. It never represents a P-D or E-P-D combination.
#[derive(Debug, Clone, PartialEq)]
pub struct RouteCandidate {
    /// Stable route target identity used by Router and Model Server Registry.
    pub route_target_id: RouteTargetId,
    /// Control-plane scaling target that owns this route target.
    pub target: ScalingTarget,
    /// Aggregate, Prefill, Decode, or Encoder execution role.
    pub role: ModelServerRole,
    /// Model served by this route target.
    pub model: String,
    /// Route target model revision.
    pub revision: String,
    /// E/P/D execution domain, if this routable ModelGroup participates in one.
    pub domain_id: Option<String>,
    /// Exact data-parallel replica selected within the route target.
    pub data_parallel_rank: u32,
    /// Latest route-target aggregate load snapshot. This is not per-rank telemetry.
    pub route_target_load: Option<RouteTargetLoad>,
}

impl RouteCandidate {
    /// Converts this internal scored candidate into the execution decision exposed by Router.
    pub(crate) fn decision(&self) -> crate::RouteDecision {
        crate::RouteDecision {
            route_target_id: self.route_target_id.clone(),
            role: self.role,
            model: self.model.clone(),
            revision: self.revision.clone(),
            data_parallel_rank: self.data_parallel_rank,
        }
    }
}

/// Lexicographically ordered route score; larger values are preferred.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, PartialOrd, Ord)]
pub struct RouteScore {
    /// Complete prompt tokens in the best readable prefix.
    pub matched_tokens: i64,
    /// Storage preference after equal prefix length: Device > HostPinned > Disk > External.
    pub tier_preference: i8,
    /// Placement locality after equal prefix and tier: Local > Remote.
    pub locality_preference: i8,
    /// Final tie breaker; built-ins negate load so less-loaded route targets rank higher.
    pub load: i64,
}

/// One candidate paired with the score produced by a `RouteScorer`.
#[derive(Debug, Clone, PartialEq)]
pub struct ScoredCandidate {
    /// Routable ModelGroup scored in the current routing round.
    pub candidate: RouteCandidate,
    /// Lexicographic score assigned by the Scorer.
    pub score: RouteScore,
}

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! One routable ModelGroup candidate and its immutable routing-round observation.

use std::sync::Arc;

use foretoken_model_protocol::ModelServerRole;

use crate::{RouteTargetId, RouteTargetStats, ScalingTarget};

/// Position in the candidate slice passed to a routing algorithm.
///
/// Filters retain positions from their input snapshot and Pickers select positions from their
/// current scored slice. Algorithms cannot manufacture or modify a candidate through this type.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct CandidateIndex(pub usize);

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
    /// E/P/D linked route-set identity, if this routable ModelGroup participates in one.
    pub pipeline_scope_id: Option<String>,
    /// Exact data-parallel replica selected within the route target.
    pub data_parallel_rank: u32,
    /// Latest route-target observation for this routing round, when telemetry covers the Router
    /// observation window. It is aggregate telemetry shared by every DP rank of this target.
    pub route_target_stats: Option<Arc<RouteTargetStats>>,
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
    /// Final tie breaker; the provided load scorers negate load so lower values rank higher.
    pub load: i64,
}

/// Router-owned view of a candidate and the parallel score produced by a `RouteScorer`.
#[derive(Debug, Clone, PartialEq)]
pub struct ScoredCandidate {
    /// Routable ModelGroup scored in the current routing round.
    pub candidate: RouteCandidate,
    /// Lexicographic score assigned by the Scorer.
    pub score: RouteScore,
}

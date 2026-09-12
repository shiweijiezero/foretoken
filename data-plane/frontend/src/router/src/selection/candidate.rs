// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! One routable ModelGroup candidate and its immutable routing-round observation.

use std::sync::Arc;

use foretoken_model_protocol::ModelServerRole;

use crate::{RouteTargetId, RouteTargetSet, RouteTargetStats, ScalingTarget};

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
    /// Complete capacity set attributed when this route is selected.
    pub admission_targets: RouteTargetSet,
    /// Aggregate, Prefill, Decode, or Encoder execution role.
    pub role: ModelServerRole,
    /// Model served by this route target.
    pub model: String,
    /// Route target model revision.
    pub revision: String,
    /// E/P/D route-set identity, if this routable ModelGroup participates in one.
    pub pipeline_scope_id: Option<String>,
    /// Exact data-parallel replica selected within the route target.
    pub data_parallel_rank: u32,
    /// Latest route-target gauges and available windowed statistics for this routing round.
    /// It is aggregate telemetry shared by every DP rank of this target.
    pub route_target_stats: Option<Arc<RouteTargetStats>>,
}

impl RouteCandidate {
    /// Returns the required execution roles after this candidate for routing algorithms.
    pub fn future_stages(&self) -> &'static [ModelServerRole] {
        match self.role {
            ModelServerRole::Aggregate | ModelServerRole::Decode => &[],
            ModelServerRole::Prefill => &[ModelServerRole::Decode],
            ModelServerRole::Encoder => &[ModelServerRole::Prefill, ModelServerRole::Decode],
        }
    }

    /// Converts this internal scored candidate into the execution decision exposed by Router.
    pub(crate) fn decision(&self) -> crate::RouteDecision {
        crate::RouteDecision {
            route_target_id: self.route_target_id.clone(),
            admission_targets: self.admission_targets.clone(),
            role: self.role,
            model: self.model.clone(),
            revision: self.revision.clone(),
            data_parallel_rank: self.data_parallel_rank,
        }
    }
}

/// Numeric preference followed by lexicographic locality and load; larger values are preferred.
#[derive(Debug, Clone, Copy, Default)]
pub struct RouteScore {
    /// Raw numeric scorer output. Locality policies leave this at zero; metric policies leave the
    /// remaining fields at zero so their floating-point scores reach Picker without quantization.
    pub preference: f64,
    /// Complete prompt tokens in the best readable prefix.
    pub matched_tokens: i64,
    /// Storage preference after equal prefix length: Device > HostPinned > Disk > External.
    pub tier_preference: i8,
    /// Placement locality after equal prefix and tier: Local > Remote.
    pub locality_preference: i8,
    /// Final tie breaker; the provided load scorers negate load so lower values rank higher.
    pub load: i64,
}

impl PartialEq for RouteScore {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other).is_eq()
    }
}

impl Eq for RouteScore {}

impl PartialOrd for RouteScore {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for RouteScore {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.preference.total_cmp(&other.preference).then_with(|| {
            (
                self.matched_tokens,
                self.tier_preference,
                self.locality_preference,
                self.load,
            )
                .cmp(&(
                    other.matched_tokens,
                    other.tier_preference,
                    other.locality_preference,
                    other.load,
                ))
        })
    }
}

/// Router-owned view of a candidate and the parallel score produced by a `RouteScorer`.
#[derive(Debug, Clone, PartialEq)]
pub struct ScoredCandidate {
    /// Routable ModelGroup scored in the current routing round.
    pub candidate: RouteCandidate,
    /// Numeric or locality preference assigned by the Scorer.
    pub score: RouteScore,
}

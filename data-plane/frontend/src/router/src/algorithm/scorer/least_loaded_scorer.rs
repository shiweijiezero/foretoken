// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Built-in scoring by current route target load.

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;

use super::{decode_loads_by_domain, load};
use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, ScoredCandidate};

/// Prefers lower current load and includes the least-loaded Decode node when scoring Prefill.
#[derive(Default)]
pub struct LeastLoadedScorer;

impl RouteScorer for LeastLoadedScorer {
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: Vec<RouteCandidate>,
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        route_target_stats_reader: &dyn crate::RouteTargetStatsReader,
        customized_context: &mut (),
    ) -> Vec<ScoredCandidate> {
        // A Prefill candidate includes the lightest Decode load in its own execution domain. The
        // score compares domains without prematurely binding to one Decode rank.
        let decode_loads = decode_loads_by_domain(&candidates);

        candidates
            .into_iter()
            .map(|candidate| {
                let downstream_load = if candidate.role == ModelServerRole::Prefill {
                    decode_loads.get(&candidate.domain_id).copied().unwrap_or(0)
                } else {
                    0
                };
                // Picker prefers larger scores, so negate the total load: less load ranks higher.
                ScoredCandidate {
                    score: RouteScore {
                        matched_tokens: 0,
                        tier_preference: 0,
                        locality_preference: 0,
                        load: load(&candidate)
                            .saturating_add(downstream_load)
                            .saturating_neg(),
                    },
                    candidate,
                }
            })
            .collect()
    }
}

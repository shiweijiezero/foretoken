// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring daily mean request lengths with twice the weight on prompt tokens.

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};
use foretoken_kv_indexer::KvPrefixIndexer;

/// Prefers smaller weighted request lengths using AIBrix's throughput cost.
#[derive(Default)]
pub struct ThroughputScorer;

impl RouteScorer for ThroughputScorer {
    fn needs_request_length_history(&self) -> bool {
        true
    }

    /// Ranks measured costs and explores idle targets until their request history is available.
    fn score(
        &self,
        _request: &RouterRequest,
        candidates: &[RouteCandidate],
        _kv_prefix_indexer: &dyn KvPrefixIndexer,
        _routing_progress: &RoutingProgress<'_>,
        _customized_context: &mut (),
    ) -> Vec<RouteScore> {
        candidates
            .iter()
            .map(|candidate| {
                let cost = candidate
                    .route_target_stats
                    .as_ref()
                    .and_then(|stats| {
                        Some(
                            2.0 * stats.prompt_tokens_per_request?
                                + stats.generation_tokens_per_request?,
                        )
                    })
                    .filter(|cost| cost.is_finite());
                // Measured targets retain their cost ordering in (0, 1]. Router's
                // reservations keep concurrent exploration from piling onto one cold target.
                let preference = match cost {
                    Some(cost) => 1.0 / (1.0 + cost),
                    None if candidate.local_load.requests == 0 => 2.0,
                    None => -(candidate.local_load.requests as f64),
                };
                RouteScore { preference }
            })
            .collect()
    }
}

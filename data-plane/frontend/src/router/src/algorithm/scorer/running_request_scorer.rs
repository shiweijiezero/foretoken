// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring by the scheduler running-request gauge.

use foretoken_kv_indexer::KvPrefixIndexer;

use super::inverse_normalized_scores;
use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};

/// Scores each candidate by `(max_running - running) / (max_running - min_running)`.
/// Equal counts receive one; only scheduler running requests contribute to the score.
#[derive(Default)]
pub struct RunningRequestScorer;

impl RouteScorer for RunningRequestScorer {
    /// Returns running-request preferences in candidate order for Router selection.
    /// Unobserved gauges count as zero; equal counts receive one.
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut (),
    ) -> Vec<RouteScore> {
        inverse_normalized_scores(candidates.iter().map(|candidate| {
            candidate
                .route_target_stats
                .as_deref()
                .and_then(|stats| stats.scheduler_running_requests)
                .unwrap_or(0)
        }))
    }
}

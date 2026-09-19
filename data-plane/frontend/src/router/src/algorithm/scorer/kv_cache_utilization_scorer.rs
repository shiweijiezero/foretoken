// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring by measured KV-cache utilization.

use foretoken_kv_indexer::KvPrefixIndexer;

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};

/// Returns `1 - utilization` for each candidate without clamping or adding other signals.
/// An unobserved gauge ranks after measured utilization.
#[derive(Default)]
pub struct KvCacheUtilizationScorer;

impl RouteScorer for KvCacheUtilizationScorer {
    /// Returns utilization preferences in candidate order for Router selection.
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut (),
    ) -> Vec<RouteScore> {
        candidates
            .iter()
            .map(|candidate| RouteScore {
                preference: candidate.data_parallel_stats()
                    .and_then(|stats| stats.kv_cache_usage)
                    .map_or(f64::NEG_INFINITY, |usage| 1.0 - usage),
                ..RouteScore::default()
            })
            .collect()
    }
}

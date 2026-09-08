// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring by the scheduler waiting-request gauge.

use foretoken_kv_indexer::KvPrefixIndexer;

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};

/// Scores each candidate by `(max_waiting - waiting) / (max_waiting - min_waiting)`.
/// Equal counts receive one; Router retains ownership of stage eligibility and picking.
#[derive(Default)]
pub struct QueueDepthScorer;

impl RouteScorer for QueueDepthScorer {
    /// Returns queue-depth preferences in candidate order for Router selection.
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
        let counts = candidates
            .iter()
            .map(|candidate| {
                candidate
                    .route_target_stats
                    .as_deref()
                    .and_then(|stats| stats.scheduler_waiting_requests)
                    .unwrap_or(0)
            })
            .collect::<Vec<_>>();
        let Some(minimum) = counts.iter().copied().min() else {
            return Vec::new();
        };
        let maximum = counts.iter().copied().max().expect("nonempty counts");
        // Subtract integer counts before conversion to preserve large adjacent differences.
        counts
            .into_iter()
            .map(|count| RouteScore {
                preference: if maximum == minimum {
                    1.0
                } else {
                    (maximum - count) as f64 / (maximum - minimum) as f64
                },
                ..RouteScore::default()
            })
            .collect()
    }
}

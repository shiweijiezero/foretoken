// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Picker for the highest-scored candidate.

use crate::{CandidateIndex, RoutePicker, RouterRequest, ScoredCandidate};

/// Selects the maximum score and smallest target ID, balancing equally scored ranks by load.
#[derive(Default)]
pub struct MaxPicker;

impl RoutePicker for MaxPicker {
    #[allow(unused_variables)]
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        routing_progress: &crate::RoutingProgress<'_>,
        customized_context: &mut (),
    ) -> Option<CandidateIndex> {
        let pressure = |scored: &ScoredCandidate| {
            let scheduler = scored
                .candidate
                .data_parallel_stats()
                .and_then(|stats| {
                    Some(
                        stats
                            .scheduler_running_requests?
                            .saturating_add(stats.scheduler_waiting_requests?),
                    )
                })
                .unwrap_or(0);
            scheduler.max(scored.candidate.local_load.requests.max(0) as u64)
        };
        let compare = |left: &ScoredCandidate, right: &ScoredCandidate| {
            left.score
                .cmp(&right.score)
                .then_with(|| {
                    right
                        .candidate
                        .route_target_id
                        .cmp(&left.candidate.route_target_id)
                })
                .then_with(|| pressure(right).cmp(&pressure(left)))
        };
        let best = scored_candidates
            .iter()
            .max_by(|left, right| compare(left, right))?;
        let tied = scored_candidates
            .iter()
            .enumerate()
            .filter(|(_, candidate)| compare(candidate, best).is_eq())
            .map(|(index, _)| CandidateIndex(index))
            .collect::<Vec<_>>();
        Some(tied[fastrand::usize(0..tied.len())])
    }
}

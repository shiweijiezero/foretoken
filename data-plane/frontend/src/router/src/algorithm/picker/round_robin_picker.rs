// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Built-in round-robin tie breaking among highest-scored candidates.

use std::sync::atomic::{AtomicUsize, Ordering};

use crate::{RouteCandidate, RoutePicker, RouterRequest, ScoredCandidate};

/// Rotates across candidates tied for the maximum score.
#[derive(Default)]
pub struct RoundRobinPicker {
    next: AtomicUsize,
}
impl RoutePicker for RoundRobinPicker {
    #[allow(unused_variables)]
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        customized_context: &mut (),
    ) -> Option<RouteCandidate> {
        let best_score = scored_candidates
            .iter()
            .map(|scored_candidate| scored_candidate.score)
            .max()?;
        let mut best_candidates = scored_candidates
            .iter()
            .filter(|scored_candidate| scored_candidate.score == best_score)
            .collect::<Vec<_>>();
        best_candidates.sort_by(|left, right| {
            left.candidate
                .route_target_id
                .cmp(&right.candidate.route_target_id)
        });
        Some(
            best_candidates[self.next.fetch_add(1, Ordering::Relaxed) % best_candidates.len()]
                .candidate
                .clone(),
        )
    }
}

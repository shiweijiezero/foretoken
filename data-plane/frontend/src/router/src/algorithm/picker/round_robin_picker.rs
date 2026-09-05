// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Round-robin tie breaking among highest-scored candidates.

use std::sync::atomic::{AtomicUsize, Ordering};

use crate::{CandidateIndex, PickerDescriptor, RoutePicker, RouterRequest, ScoredCandidate};

inventory::submit! {
    PickerDescriptor {
        name: "round_robin",
        factory: || std::sync::Arc::new(RoundRobinPicker::default()),
    }
}

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
    ) -> Option<CandidateIndex> {
        let best_score = scored_candidates
            .iter()
            .map(|scored_candidate| scored_candidate.score)
            .max()?;
        let mut best_indexes = scored_candidates
            .iter()
            .enumerate()
            .filter(|(_, scored_candidate)| scored_candidate.score == best_score)
            .map(|(index, _)| index)
            .collect::<Vec<_>>();
        best_indexes.sort_by(|left, right| {
            scored_candidates[*left]
                .candidate
                .route_target_id
                .cmp(&scored_candidates[*right].candidate.route_target_id)
        });
        Some(CandidateIndex(
            best_indexes[self.next.fetch_add(1, Ordering::Relaxed) % best_indexes.len()],
        ))
    }
}

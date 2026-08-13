// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Picker for the highest-scored candidate.

use std::sync::Arc;

use crate::{CandidateIndex, PickerDescriptor, RoutePicker, RouterRequest, ScoredCandidate};

inventory::submit! {
    PickerDescriptor {
        name: "max",
        factory: || Arc::new(MaxPicker),
    }
}

/// Selects the maximum score, breaking ties by the smallest route target ID.
#[derive(Default)]
pub struct MaxPicker;

impl RoutePicker for MaxPicker {
    #[allow(unused_variables)]
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        customized_context: &mut (),
    ) -> Option<CandidateIndex> {
        scored_candidates
            .iter()
            .enumerate()
            .max_by(|(_, left), (_, right)| {
                left.score.cmp(&right.score).then_with(|| {
                    right
                        .candidate
                        .route_target_id
                        .cmp(&left.candidate.route_target_id)
                })
            })
            .map(|(index, _)| CandidateIndex(index))
    }
}

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Built-in picker for the highest-scored candidate.

use crate::{RouteCandidate, RoutePicker, RouterRequest, ScoredCandidate};

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
    ) -> Option<RouteCandidate> {
        scored_candidates
            .iter()
            .max_by(|left, right| {
                left.score.cmp(&right.score).then_with(|| {
                    right
                        .candidate
                        .route_target_id
                        .cmp(&left.candidate.route_target_id)
                })
            })
            .map(|scored_candidate| scored_candidate.candidate.clone())
    }
}

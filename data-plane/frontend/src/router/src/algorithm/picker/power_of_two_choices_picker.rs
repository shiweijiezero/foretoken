// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Power-of-two-choices selection for lower routing oscillation.

use crate::{CandidateIndex, RoutePicker, RouterRequest, ScoredCandidate};

/// Samples two candidates and selects the higher-scored one.
#[derive(Default)]
pub struct PowerOfTwoChoicesPicker;

impl RoutePicker for PowerOfTwoChoicesPicker {
    #[allow(unused_variables)]
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        routing_progress: &crate::RoutingProgress<'_>,
        customized_context: &mut (),
    ) -> Option<CandidateIndex> {
        let length = scored_candidates.len();
        if length == 0 {
            return None;
        }
        if length == 1 {
            return Some(CandidateIndex(0));
        }

        let first = fastrand::usize(..length);
        let second = (first + 1 + fastrand::usize(..length - 1)) % length;
        let ordering = scored_candidates[first]
            .score
            .cmp(&scored_candidates[second].score);
        if ordering.is_gt() {
            Some(CandidateIndex(first))
        } else if ordering.is_lt() {
            Some(CandidateIndex(second))
        } else if fastrand::bool() {
            Some(CandidateIndex(first))
        } else {
            Some(CandidateIndex(second))
        }
    }
}

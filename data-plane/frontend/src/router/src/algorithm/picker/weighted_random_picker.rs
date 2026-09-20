// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Probability sampling from the complete route-score ordering.

use crate::{CandidateIndex, RoutePicker, RouterRequest, ScoredCandidate};

/// Samples candidates by descending `RouteScore` rank instead of discarding non-best scores.
#[derive(Default)]
pub struct WeightedRandomPicker;

impl RoutePicker for WeightedRandomPicker {
    #[allow(unused_variables)]
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        routing_progress: &crate::RoutingProgress<'_>,
        customized_context: &mut (),
    ) -> Option<CandidateIndex> {
        if scored_candidates.is_empty() {
            return None;
        }

        let mut ranked = (0..scored_candidates.len()).collect::<Vec<_>>();
        ranked.sort_by(|left, right| {
            scored_candidates[*right]
                .score
                .cmp(&scored_candidates[*left].score)
                .then_with(|| {
                    scored_candidates[*left]
                        .candidate
                        .route_target_id
                        .cmp(&scored_candidates[*right].candidate.route_target_id)
                })
        });

        let mut weights = vec![0.0; scored_candidates.len()];
        for (position, index) in ranked.iter().copied().enumerate() {
            let weight = (ranked.len() - position) as f64;
            if position == 0
                || scored_candidates[index].score != scored_candidates[ranked[position - 1]].score
            {
                weights[index] = weight;
            } else {
                weights[index] = weights[ranked[position - 1]];
            }
        }

        let total = weights.iter().sum::<f64>();
        let mut threshold = fastrand::f64() * total;
        for (index, weight) in weights.into_iter().enumerate() {
            if threshold < weight {
                return Some(CandidateIndex(index));
            }
            threshold -= weight;
        }
        Some(CandidateIndex(scored_candidates.len() - 1))
    }
}

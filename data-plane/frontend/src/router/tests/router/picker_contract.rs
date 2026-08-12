// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Picker output contract tests.

use std::sync::Arc;

use foretoken_model_protocol::ModelServerRole;

use super::support::{inventory, request, route};
use foretoken_router::algorithm::{AllowAllFilter, UniformScorer};
use foretoken_router::{
    PipelineRouter, RouteCandidate, RouteError, RoutePicker, RouteTargetId, Router, RouterPipeline,
    RouterRequest, ScoredCandidate,
};

struct InvalidPicker;

impl RoutePicker for InvalidPicker {
    #[allow(unused_variables)]
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        customized_context: &mut (),
    ) -> Option<RouteCandidate> {
        let mut candidate = scored_candidates.first()?.candidate.clone();
        candidate.route_target_id = RouteTargetId::new("not-in-round");
        Some(candidate)
    }
}

#[test]
fn picker_must_return_a_candidate_from_the_current_stage() {
    let (inventory, _) = inventory(vec![route("a", ModelServerRole::Aggregate)]);
    let router = PipelineRouter::with_pipeline(
        inventory,
        RouterPipeline::new(
            Arc::new(AllowAllFilter),
            Arc::new(UniformScorer),
            Arc::new(InvalidPicker),
        ),
    );

    assert_eq!(
        router.start(request()).select_initial(),
        Err(RouteError::InvalidPickerResult)
    );
}

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Aggregate, P/D, and E/P/D routing session tests.

use std::sync::Arc;

use foretoken_model_protocol::ModelServerRole;

use super::support::{inventory, inventory_with_unhealthy, request, route};
use foretoken_router::algorithm::{AllowAllFilter, LeastLoadedScorer, MaxPicker};
use foretoken_router::{
    PipelineRouter, RouteError, RouteTargetId, RouteTargetLoad, Router, RouterPipeline,
};

#[test]
fn single_rank_route_returns_an_explicit_rank_zero_decision() {
    let (inventory, _) = inventory(vec![route("a", ModelServerRole::Aggregate)]);
    let router = PipelineRouter::new(inventory);
    let mut session = router.start(request());

    let decision = session.select_initial().unwrap();
    assert_eq!(decision.route_target_id, RouteTargetId::new("a"));
    assert_eq!(decision.role, ModelServerRole::Aggregate);
    assert_eq!(decision.data_parallel_rank, 0);
    assert_eq!(
        session.select_decode(),
        Err(RouteError::DecodeBeforePrefill)
    );
}

#[test]
fn prefill_is_not_selected_without_an_available_decode() {
    let (inventory, _) = inventory(vec![route("p", ModelServerRole::Prefill)]);
    let router = PipelineRouter::new(inventory);
    let mut session = router.start(request());

    assert!(matches!(
        session.select_initial(),
        Err(RouteError::NoMatchingRouteTarget { .. })
    ));
}

#[test]
fn encoder_prefill_decode_stays_in_its_domain_and_never_falls_back_without_encoder() {
    let (inventory, _) = inventory(vec![
        route("e", ModelServerRole::Encoder),
        route("p", ModelServerRole::Prefill),
        route("d", ModelServerRole::Decode),
    ]);
    let router = PipelineRouter::new(inventory);
    let mut session = router.start(request());
    assert_eq!(
        session.select_initial().unwrap().route_target_id.as_str(),
        "e"
    );
    assert_eq!(
        session.select_prefill().unwrap().route_target_id.as_str(),
        "p"
    );
    assert_eq!(
        session.select_decode().unwrap().route_target_id.as_str(),
        "d"
    );

    let (inventory, _) = inventory_with_unhealthy(
        vec![
            route("e", ModelServerRole::Encoder),
            route("p", ModelServerRole::Prefill),
            route("d", ModelServerRole::Decode),
        ],
        [RouteTargetId::new("e")].into_iter().collect(),
    );
    assert!(matches!(
        PipelineRouter::new(inventory)
            .start(request())
            .select_initial(),
        Err(RouteError::NoMatchingRouteTarget { .. })
    ));
}

#[test]
fn decode_uses_fresh_load_and_is_not_bound_during_prefill_selection() {
    let (inventory, loads) = inventory(vec![
        route("p", ModelServerRole::Prefill),
        route("d1", ModelServerRole::Decode),
        route("d2", ModelServerRole::Decode),
    ]);
    let router = PipelineRouter::with_pipeline(
        inventory,
        RouterPipeline::new(
            Arc::new(AllowAllFilter),
            Arc::new(LeastLoadedScorer),
            Arc::new(MaxPicker),
        ),
    );
    let mut session = router.start(request());
    assert_eq!(
        session.select_initial().unwrap().route_target_id.as_str(),
        "p"
    );

    loads.lock().unwrap().extend([
        (
            RouteTargetId::new("d1"),
            RouteTargetLoad {
                running_requests: Some(10),
            },
        ),
        (
            RouteTargetId::new("d2"),
            RouteTargetLoad {
                running_requests: Some(1),
            },
        ),
    ]);
    assert_eq!(
        session.select_decode().unwrap().route_target_id.as_str(),
        "d2"
    );
}

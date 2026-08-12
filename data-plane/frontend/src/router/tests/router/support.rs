// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared fixtures for Router integration tests.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use foretoken_model_protocol::ModelServerRole;
use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;

use foretoken_router::{
    ModelRouteTable, RouteInventory, RouteTarget, RouteTargetId, RouteTargetLoad, RouterRequest,
    ScalingTarget, ScalingTargetKind,
};

pub(super) type Loads = Arc<Mutex<BTreeMap<RouteTargetId, RouteTargetLoad>>>;

pub(super) struct TestInventory {
    model_routes: ModelRouteTable,
    loads: Loads,
    unhealthy: BTreeSet<RouteTargetId>,
}

impl RouteInventory for TestInventory {
    fn model_routes(&self) -> &ModelRouteTable {
        &self.model_routes
    }

    fn is_route_target_healthy(&self, route_target_id: &RouteTargetId) -> bool {
        !self.unhealthy.contains(route_target_id)
    }

    fn route_target_load(&self, route_target_id: &RouteTargetId) -> Option<RouteTargetLoad> {
        self.loads.lock().unwrap().get(route_target_id).cloned()
    }
}

pub(super) fn route(id: &str, role: ModelServerRole) -> RouteTarget {
    RouteTarget {
        route_target_id: RouteTargetId::new(id),
        target: ScalingTarget {
            service_uid: "service".into(),
            name: id.into(),
            uid: format!("{id}-uid"),
            kind: ScalingTargetKind::Pool,
        },
        model: "model".into(),
        revision: "r1".into(),
        capabilities: BTreeSet::new(),
        max_input_tokens: None,
        ready: true,
        role,
        domain_id: matches!(
            role,
            ModelServerRole::Prefill | ModelServerRole::Decode | ModelServerRole::Encoder
        )
        .then(|| "domain-a".into()),
        data_parallel_size: 1,
    }
}

pub(super) fn request() -> RouterRequest {
    RouterRequest::new(
        "model",
        Some("r1".into()),
        Arc::new(vllm_llm::GenerateRequest {
            request_id: "request".into(),
            prompt_token_ids: vec![1, 2],
            sampling_params: EngineCoreSamplingParams::default(),
            mm_features: None,
            arrival_time: None,
            cache_salt: None,
            trace_headers: None,
            priority: 0,
            data_parallel_rank: None,
            reasoning_parser_kwargs: None,
            lora_request: None,
        }),
    )
}

pub(super) fn inventory(routes: Vec<RouteTarget>) -> (Arc<TestInventory>, Loads) {
    inventory_with_unhealthy(routes, BTreeSet::new())
}

pub(super) fn inventory_with_unhealthy(
    routes: Vec<RouteTarget>,
    unhealthy: BTreeSet<RouteTargetId>,
) -> (Arc<TestInventory>, Loads) {
    let loads = Arc::new(Mutex::new(BTreeMap::new()));
    (
        Arc::new(TestInventory {
            model_routes: ModelRouteTable::new(routes),
            loads: loads.clone(),
            unhealthy,
        }),
        loads,
    )
}

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared fixtures for Router integration tests.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use foretoken_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
use foretoken_model_protocol::ModelServerRole;

use foretoken_router::{
    ModelRouteTable, RouteInventory, RouteTarget, RouteTargetId, RouteTargetSet, RouteTargetStats,
    RouterRequest, ScalingTarget, ScalingTargetKind,
};

pub(super) type Stats = Arc<Mutex<BTreeMap<RouteTargetId, RouteTargetStats>>>;

pub(super) struct TestInventory {
    model_routes: ModelRouteTable,
    unhealthy: BTreeSet<RouteTargetId>,
}

impl RouteInventory for TestInventory {
    fn model_routes(&self) -> &ModelRouteTable {
        &self.model_routes
    }

    fn is_route_target_healthy(&self, route_target_id: &RouteTargetId) -> bool {
        !self.unhealthy.contains(route_target_id)
    }
}

pub(super) struct TestStatsReader {
    stats: Stats,
}

impl TestStatsReader {
    pub(super) fn new(stats: Stats) -> Self {
        Self { stats }
    }
}

impl foretoken_router::RouteTargetStatsReader for TestStatsReader {
    fn stats(&self, route_target_id: &RouteTargetId, _: Duration) -> Option<RouteTargetStats> {
        self.stats.lock().unwrap().get(route_target_id).cloned()
    }
}

pub(super) fn route(id: &str, role: ModelServerRole) -> RouteTarget {
    let target = ScalingTarget {
        service_uid: "service".into(),
        name: id.into(),
        uid: format!("{id}-uid"),
        kind: ScalingTargetKind::Pool,
    };
    RouteTarget {
        route_target_id: RouteTargetId::new(id),
        admission_targets: RouteTargetSet::new(vec![target.clone()]),
        target,
        model: "model".into(),
        revision: "r1".into(),
        capabilities: BTreeSet::new(),
        max_input_tokens: None,
        ready: true,
        role,
        pipeline_scope_id: matches!(
            role,
            ModelServerRole::Prefill | ModelServerRole::Decode | ModelServerRole::Encoder
        )
        .then(|| "pipeline-scope-a".into()),
        data_parallel_size: 1,
    }
}

pub(super) fn request() -> RouterRequest {
    RouterRequest::new(
        "model",
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
            session_id: None,
            reasoning_parser_kwargs: None,
            lora_request: None,
        }),
    )
}

pub(super) fn inventory(routes: Vec<RouteTarget>) -> Arc<TestInventory> {
    inventory_with_unhealthy(routes, BTreeSet::new())
}

pub(super) fn inventory_with_unhealthy(
    routes: Vec<RouteTarget>,
    unhealthy: BTreeSet<RouteTargetId>,
) -> Arc<TestInventory> {
    Arc::new(TestInventory {
        model_routes: ModelRouteTable::new(routes),
        unhealthy,
    })
}

pub(super) fn stats() -> Stats {
    Arc::new(Mutex::new(BTreeMap::new()))
}

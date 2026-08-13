// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::collections::BTreeMap;
use std::sync::Arc;

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

use crate::{
    Generation, GenerationError, GenerationRequest, RuntimeControl, RuntimeGeneration,
    RuntimeState, router,
};
use foretoken_backend_registry::BackendRegistry;
use foretoken_chat::ParserSelection;
use foretoken_router::{PipelineRouter, RouteTargetSet, ScalingTarget, ScalingTargetKind};
use foretoken_text::{Prompt, SamplingParams, TextDecodeOptions};

use super::support::{RecordingGeneration, generation_state, test_runtime};

struct StaticRuntimeControl {
    ready: bool,
    models: Vec<String>,
}

#[async_trait]
impl RuntimeControl for StaticRuntimeControl {
    async fn refresh_backend_readiness(&self) {}

    fn healthy_models(&self) -> Vec<String> {
        self.models.clone()
    }

    fn is_ready(&self) -> bool {
        self.ready
    }
}

#[test]
fn runtime_state_selects_model_and_requires_matching_revision() {
    let registry = Arc::new(
        BackendRegistry::from_json(
            br#"{"version":1,"groups":[{"service_uid":"service","pool_uid":"pool","pool_name":"pool","route_target_id":"a","model":"model-a","revision":"r1","tokenizer":"a","tokenizer_revision":"r1","endpoint":"http://127.0.0.1:1","data_parallel_size":1}]}"#,
        )
        .unwrap(),
    );
    let state = RuntimeState::new(
        std::collections::BTreeMap::from([
            ("model-a".into(), test_runtime("r1")),
            ("model-b".into(), test_runtime("r2")),
        ]),
        Arc::new(PipelineRouter::new(registry.clone())),
        registry,
    );

    assert_eq!(state.model("model-b", None).unwrap().revision(), "r2");
    assert!(state.model("model-b", Some("r1")).is_err());
    assert!(state.model("missing", None).is_err());
}

#[test]
fn runtime_generation_publishes_atomically_and_rejects_stale_versions() {
    let generation = RuntimeGeneration::new();
    assert!(!generation.ready());
    assert_eq!(generation.active_version(), None);

    assert!(generation.replace_state(
        2,
        generation_state("new", "r2"),
        Arc::new(StaticRuntimeControl {
            ready: true,
            models: vec!["new".into()],
        }),
    ));
    assert!(generation.ready());
    assert_eq!(generation.models(), vec!["new"]);
    assert_eq!(generation.active_version(), Some(2));

    assert!(!generation.replace_state(
        1,
        generation_state("old", "r1"),
        Arc::new(StaticRuntimeControl {
            ready: false,
            models: vec!["old".into()],
        }),
    ));
    assert!(generation.ready());
    assert_eq!(generation.models(), vec!["new"]);
    assert_eq!(generation.active_version(), Some(2));

    assert!(generation.replace_state(
        3,
        generation_state("next", "r3"),
        Arc::new(StaticRuntimeControl {
            ready: false,
            models: vec!["next".into()],
        }),
    ));
    assert!(!generation.ready());
    assert_eq!(generation.models(), vec!["next"]);
    assert_eq!(generation.active_version(), Some(3));
}

#[tokio::test]
async fn logical_only_runtime_returns_unavailable_without_queueing() {
    let registry = Arc::new(
        BackendRegistry::from_json(
            br#"{"version":1,"models":[{"service_uid":"service","model":"model","revision":"r1","tokenizer":"model","tokenizer_revision":"r1","targets":[{"service_uid":"service","name":"default","uid":"pool","kind":"Pool"}]}],"groups":[]}"#,
        )
        .unwrap(),
    );
    let target = ScalingTarget {
        service_uid: "service".into(),
        name: "default".into(),
        uid: "pool".into(),
        kind: ScalingTargetKind::Pool,
    };
    let state = RuntimeState::new(
        BTreeMap::new(),
        Arc::new(PipelineRouter::new(registry.clone())),
        registry,
    )
    .with_logical_targets(BTreeMap::from([(
        "model".into(),
        RouteTargetSet::new(vec![target.clone()]),
    )]));
    let generation = Arc::new(RuntimeGeneration::new());
    assert!(generation.replace_state(
        1,
        Arc::new(state),
        Arc::new(StaticRuntimeControl {
            ready: true,
            models: vec!["model".into()],
        }),
    ));
    let request = GenerationRequest {
        model: "model".into(),
        request_id: "cold-start".into(),
        revision: None,
        prompt: Prompt::Text("hello".into()),
        sampling_params: SamplingParams::default(),
        decode_options: TextDecodeOptions::default(),
        intermediate: false,
        priority: 0,
        cache_salt: None,
        session_id: None,
        arrival_time: None,
        tool_call_parser: ParserSelection::None,
        reasoning_parser: ParserSelection::None,
    };
    assert!(matches!(
        generation.generate(request).await,
        Err(GenerationError::Unavailable)
    ));
    assert!(
        foretoken_metrics::autoscaling_telemetry()
            .targets
            .iter()
            .all(|value| value.target.target_id != target.uid || value.queued_requests == 0)
    );
}

#[tokio::test]
async fn health_is_live_before_the_first_runtime_generation_is_ready() {
    let app = router(
        Arc::new(RuntimeGeneration::new()),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let health = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let readiness = app
        .oneshot(
            Request::builder()
                .uri("/readyz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(health.status(), StatusCode::OK);
    assert_eq!(readiness.status(), StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn metrics_endpoint_uses_vllm_openmetrics_registry() {
    let app = router(
        Arc::new(RecordingGeneration::default()),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let models = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/models")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(models.status(), StatusCode::OK);

    let status = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/statusz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(status.status(), StatusCode::OK);
    let status = axum::body::to_bytes(status.into_body(), usize::MAX)
        .await
        .unwrap();
    let status: serde_json::Value = serde_json::from_slice(&status).unwrap();
    assert_eq!(status["serving_ready"], true);
    assert_eq!(status["kv_index"]["state"], "degraded");
    assert_eq!(status["kv_index"]["reason"], "event_subscriber_unavailable");

    let response = app
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.headers()["content-type"],
        "application/openmetrics-text; version=1.0.0; charset=utf-8"
    );
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body = String::from_utf8(body.to_vec()).unwrap();
    assert!(body.contains("http_requests"));
    assert!(
        body.contains("foretoken_kv_index_degraded{reason=\"event_subscriber_unavailable\"} 1")
    );
}

#[tokio::test]
async fn autoscaling_telemetry_contains_only_stable_target_aggregates() {
    let targets = foretoken_router::RouteTargetSet::new(vec![foretoken_router::ScalingTarget {
        service_uid: "service-uid".into(),
        name: "aggregate".into(),
        uid: "pool-uid".into(),
        kind: foretoken_router::ScalingTargetKind::Pool,
    }]);
    foretoken_metrics::register_targets(&targets);
    let app = router(
        Arc::new(RecordingGeneration::default()),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let response = app
        .oneshot(
            Request::builder()
                .uri("/internal/autoscaling/telemetry")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let text = String::from_utf8(body.to_vec()).unwrap();
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    assert_eq!(value["version"], 1);
    assert!(value["targets"].as_array().unwrap().iter().any(|target| {
        target["service_uid"] == "service-uid"
            && target["target_kind"] == "Pool"
            && target["target_id"] == "pool-uid"
            && target["queued_requests"] == 0
    }));
    assert!(!text.contains("request_id"));
    assert!(!text.contains("route_target_id"));
    assert!(!text.contains("token"));
}

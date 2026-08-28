// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::{Json, Router, http::StatusCode, routing::get};
use foretoken_backend_registry::{
    BackendRegistry, BackendRegistryBuild, ServingSnapshot, SnapshotEpdComponent,
    SnapshotEpdPipelineScope, SnapshotError, SnapshotGroup, SnapshotModel, SnapshotPdComponent,
    SnapshotPdPipelineScope,
};
use foretoken_engine_core_client::protocol::dtype::ModelDtype;
use foretoken_llm_facade::{LlmFacadeResolver, RouteStage};
use foretoken_model_protocol::{
    CumulativeHistogram, CumulativeHistogramBucket, ModelServerRole, RuntimeMetadataResponse,
    RuntimeModelIdentity, TelemetryResponse,
};
use foretoken_router::{
    RouteDecision, RouteInventory, RouteTargetId, RouteTargetSet, RouteTargetStatsReader,
    ScalingTarget, ScalingTargetKind,
};
use tokio::net::TcpListener;

fn pd_component(id: &str, role: ModelServerRole) -> SnapshotPdComponent {
    let pool = if role == ModelServerRole::Prefill {
        "prefill"
    } else {
        "decode"
    };
    SnapshotPdComponent {
        service_uid: "service".into(),
        pool_uid: format!("{pool}-uid"),
        pool_name: pool.into(),
        route_target_id: RouteTargetId::new(id),
        role,
        pipeline_scope_id: "service-a".into(),
        model: "model".into(),
        revision: "r1".into(),
        tokenizer: "tokenizer".into(),
        tokenizer_revision: "r1".into(),
        profile_name: "profile".into(),
        profile_revision: "r1".into(),
        connector: "MooncakeConnector".into(),
        protocol: "rdma".into(),
        capabilities: ["chat".into()].into_iter().collect(),
        max_input_tokens: None,
        endpoint: "http://127.0.0.1:1".into(),
        prefill_bootstrap_endpoint: (role == ModelServerRole::Prefill)
            .then(|| "http://127.0.0.1:29001".into()),
        kv_scope_id: "scope".into(),
        data_parallel_size: 1,
    }
}

fn pd_snapshot() -> ServingSnapshot {
    let admission_targets = RouteTargetSet::new(vec![
        ScalingTarget {
            service_uid: "service".into(),
            name: "prefill".into(),
            uid: "prefill-uid".into(),
            kind: ScalingTargetKind::Pool,
        },
        ScalingTarget {
            service_uid: "service".into(),
            name: "decode".into(),
            uid: "decode-uid".into(),
            kind: ScalingTargetKind::Pool,
        },
    ]);
    ServingSnapshot {
        version: 1,
        models: vec![SnapshotModel {
            service_uid: "service".into(),
            model: "model".into(),
            revision: "r1".into(),
            tokenizer: "tokenizer".into(),
            tokenizer_revision: "r1".into(),
            capabilities: ["chat".into()].into_iter().collect(),
            admission_target_sets: vec![admission_targets],
        }],
        groups: vec![],
        pd_components: vec![
            pd_component("p", ModelServerRole::Prefill),
            pd_component("d", ModelServerRole::Decode),
        ],
        pd_pipeline_scopes: vec![SnapshotPdPipelineScope {
            pipeline_scope_id: "service-a".into(),
            prefill_route_target_ids: vec![RouteTargetId::new("p")],
            decode_route_target_ids: vec![RouteTargetId::new("d")],
        }],
        epd_components: vec![],
        epd_pipeline_scopes: vec![],
    }
}

fn epd_component(id: &str, role: ModelServerRole) -> SnapshotEpdComponent {
    let pd = role != ModelServerRole::Encoder;
    let ec = role != ModelServerRole::Decode;
    SnapshotEpdComponent {
        service_uid: "service".into(),
        pool_uid: "pool".into(),
        pool_name: "pool".into(),
        route_target_id: RouteTargetId::new(id),
        role,
        pipeline_scope_id: "epd-a".into(),
        model: "model".into(),
        revision: "r1".into(),
        tokenizer: "tokenizer".into(),
        tokenizer_revision: "r1".into(),
        profile_name: if pd {
            "pd-profile".into()
        } else {
            String::new()
        },
        profile_revision: if pd { "r1".into() } else { String::new() },
        connector: if pd {
            "MooncakeConnector".into()
        } else {
            String::new()
        },
        protocol: if pd { "rdma".into() } else { String::new() },
        ec_profile_name: if ec {
            "ec-profile".into()
        } else {
            String::new()
        },
        ec_profile_revision: if ec { "r1".into() } else { String::new() },
        ec_connector: if ec {
            "ECExampleConnector".into()
        } else {
            String::new()
        },
        capabilities: ["chat".into()].into_iter().collect(),
        max_input_tokens: None,
        endpoint: "http://127.0.0.1:1".into(),
        prefill_bootstrap_endpoint: (role == ModelServerRole::Prefill)
            .then(|| "http://127.0.0.1:29001".into()),
        kv_scope_id: "scope".into(),
        data_parallel_size: 1,
    }
}

fn epd_snapshot() -> ServingSnapshot {
    ServingSnapshot {
        version: 1,
        models: vec![],
        groups: vec![],
        pd_components: vec![],
        pd_pipeline_scopes: vec![],
        epd_components: vec![
            epd_component("e", ModelServerRole::Encoder),
            epd_component("p", ModelServerRole::Prefill),
            epd_component("d", ModelServerRole::Decode),
        ],
        epd_pipeline_scopes: vec![SnapshotEpdPipelineScope {
            pipeline_scope_id: "epd-a".into(),
            encoder_route_target_id: RouteTargetId::new("e"),
            prefill_route_target_id: RouteTargetId::new("p"),
            decode_route_target_id: RouteTargetId::new("d"),
        }],
    }
}

fn runtime_metadata() -> RuntimeMetadataResponse {
    RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: "model".into(),
            revision: "r1".into(),
        },
        model_dtype: ModelDtype::BFloat16,
        effective_max_model_len: 32_768,
        ec_transfer: None,
        capabilities: ["chat".into()].into_iter().collect(),
    }
}

fn histogram(count: u64, sum_seconds: f64, first_bucket: u64) -> CumulativeHistogram {
    CumulativeHistogram {
        count,
        sum_seconds,
        buckets: vec![
            CumulativeHistogramBucket {
                le_seconds: 0.1,
                count: first_bucket,
            },
            CumulativeHistogramBucket {
                le_seconds: 0.5,
                count,
            },
        ],
    }
}

fn telemetry(at_ms: u64, tokens: u64, histogram: CumulativeHistogram) -> TelemetryResponse {
    TelemetryResponse {
        version: 2,
        collected_at_unix_ms: at_ms,
        accepting: true,
        running_requests: 0,
        max_concurrent_requests: 1,
        scheduler_running_requests: Some(0),
        scheduler_waiting_requests: Some(0),
        kv_cache_usage: Some(0.0),
        prompt_tokens_total: Some(tokens),
        generation_tokens_total: Some(tokens / 2),
        ttft_seconds: histogram.clone(),
        tpot_seconds: histogram.clone(),
        e2e_seconds: histogram,
    }
}

async fn serve_model_server() -> String {
    serve_model_server_with_telemetry(Arc::new(Mutex::new(telemetry(1, 0, Default::default()))))
        .await
}

async fn serve_model_server_with_telemetry(telemetry: Arc<Mutex<TelemetryResponse>>) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let metadata = runtime_metadata();
    let app = Router::new()
        .route("/readyz", get(|| async { StatusCode::OK }))
        .route(
            "/query",
            get(|| async { Json(serde_json::json!({"0":{"engine_id":"engine-0"}})) }),
        )
        .route(
            "/v1/internal/metadata",
            get(move || {
                let metadata = metadata.clone();
                async move { Json(metadata) }
            }),
        )
        .route(
            "/v1/internal/telemetry",
            get(move || {
                let telemetry = telemetry.clone();
                async move { Json(telemetry.lock().unwrap().clone()) }
            }),
        );
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    endpoint
}

async fn serve_ready_without_metadata() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let app = Router::new()
        .route("/readyz", get(|| async { StatusCode::OK }))
        .route(
            "/v1/internal/metadata",
            get(|| async { StatusCode::INTERNAL_SERVER_ERROR }),
        )
        .route(
            "/v1/internal/telemetry",
            get(|| async { StatusCode::INTERNAL_SERVER_ERROR }),
        );
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    endpoint
}

fn aggregate_snapshot(endpoint: String) -> ServingSnapshot {
    ServingSnapshot {
        version: 1,
        models: vec![],
        groups: vec![SnapshotGroup {
            service_uid: "service".into(),
            pool_uid: "pool".into(),
            pool_name: "pool".into(),
            route_target_id: RouteTargetId::new("a"),
            model: "model".into(),
            revision: "r1".into(),
            tokenizer: "tokenizer".into(),
            tokenizer_revision: "r1".into(),
            capabilities: ["chat".into(), "multimodal".into()].into_iter().collect(),
            max_input_tokens: None,
            endpoint,
            kv_scope_id: "scope".into(),
            data_parallel_size: 1,
        }],
        pd_components: vec![],
        pd_pipeline_scopes: vec![],
        epd_components: vec![],
        epd_pipeline_scopes: vec![],
    }
}

// Protects frontend-owned capabilities while runtime metadata gates backend-owned capabilities.
#[tokio::test]
async fn aggregate_readiness_preserves_frontend_owned_capabilities() {
    let registry =
        BackendRegistry::from_snapshot(aggregate_snapshot(serve_model_server().await)).unwrap();

    registry.refresh_backend_readiness().await;

    assert!(registry.is_route_target_healthy(&RouteTargetId::new("a")));
    assert_eq!(
        registry.effective_capabilities(&RouteTargetId::new("a")),
        ["chat".into(), "multimodal".into()].into_iter().collect()
    );
    assert_eq!(registry.effective_max_model_len("model"), Some(32_768));
    assert_eq!(
        registry.effective_model_dtype("model"),
        Some(ModelDtype::BFloat16)
    );
}

// Protects routing from a process that is alive but reports missing or mismatched model metadata.
#[tokio::test]
async fn readiness_requires_runtime_metadata() {
    let registry =
        BackendRegistry::from_snapshot(aggregate_snapshot(serve_ready_without_metadata().await))
            .unwrap();

    registry.refresh_backend_readiness().await;

    assert!(!registry.is_route_target_healthy(&RouteTargetId::new("a")));
    let mut mismatched = aggregate_snapshot(serve_model_server().await);
    mismatched.groups[0].model = "different-model".into();
    let registry = BackendRegistry::from_snapshot(mismatched).unwrap();
    registry.refresh_backend_readiness().await;
    assert!(!registry.is_route_target_healthy(&RouteTargetId::new("a")));
}

// Protects rate windows, histogram aggregation, and counter-reset invalidation.
#[tokio::test]
async fn telemetry_history_derives_windows_and_rejects_counter_resets() {
    let telemetry_state = Arc::new(Mutex::new(telemetry(1_000, 100, histogram(2, 0.2, 1))));
    let endpoint = serve_model_server_with_telemetry(telemetry_state.clone()).await;
    let registry = BackendRegistry::from_snapshot(aggregate_snapshot(endpoint)).unwrap();
    let target = RouteTargetId::new("a");

    registry.refresh_backend_readiness().await;
    *telemetry_state.lock().unwrap() = telemetry(151_000, 400, histogram(4, 0.8, 2));
    registry.refresh_backend_readiness().await;

    let stats = registry.stats(&target, Duration::from_secs(150)).unwrap();
    assert_eq!(stats.observed_window, Duration::from_secs(150));
    assert_eq!(stats.prompt_tokens_per_second, Some(2.0));
    assert_eq!(stats.generation_tokens_per_second, Some(1.0));
    assert_eq!(stats.ttft.unwrap().p95_ms, Some(500.0));

    *telemetry_state.lock().unwrap() = telemetry(302_000, 10, histogram(1, 0.1, 1));
    registry.refresh_backend_readiness().await;
    assert!(registry.stats(&target, Duration::from_secs(150)).is_none());

    telemetry_state.lock().unwrap().accepting = false;
    registry.refresh_backend_readiness().await;
    assert!(!registry.is_route_target_healthy(&target));
}

// Protects one P/D snapshot projection across routing, readiness, admission, and KV bindings.
#[tokio::test]
async fn pd_snapshot_projects_routing_readiness_and_kv_contracts() {
    let mut partial = pd_snapshot();
    partial.pd_components[0].endpoint = serve_model_server().await;
    partial.pd_components[0].prefill_bootstrap_endpoint =
        Some(partial.pd_components[0].endpoint.clone());
    let partial_registry = BackendRegistry::from_snapshot(partial).unwrap();
    partial_registry.refresh_backend_readiness().await;
    assert!(!partial_registry.is_ready());

    let mut snapshot = pd_snapshot();
    snapshot.pd_components[0].endpoint = serve_model_server().await;
    snapshot.pd_components[0].prefill_bootstrap_endpoint =
        Some(snapshot.pd_components[0].endpoint.clone());
    snapshot.pd_components[1].endpoint = serve_model_server().await;
    let build = BackendRegistryBuild::from_snapshot(snapshot).unwrap();
    let registry = build.registry;
    registry.refresh_backend_readiness().await;

    let routes = registry.model_routes().routes();
    assert_eq!(routes.len(), 2);
    assert!(
        routes
            .iter()
            .all(|route| route.admission_targets.targets().len() == 2)
    );
    assert!(registry.is_ready());
    assert_eq!(registry.healthy_models(), vec!["model"]);

    let prefill = RouteDecision {
        route_target_id: RouteTargetId::new("p"),
        admission_targets: RouteTargetSet::default(),
        role: ModelServerRole::Prefill,
        model: "model".into(),
        revision: "r1".into(),
        data_parallel_rank: 0,
    };
    assert!(
        registry
            .resolve_stage(&prefill, RouteStage::Prefill)
            .is_some()
    );
    assert!(
        registry
            .resolve_stage(&prefill, RouteStage::Decode)
            .is_none()
    );

    assert_eq!(build.kv_runtime_config.route_bindings.len(), 1);
    assert_eq!(build.kv_runtime_config.event_sources.len(), 1);
    assert!(build.kv_runtime_config.route_bindings.contains_key("p"));
    assert_eq!(
        build.kv_runtime_config.event_sources[0].event_source_id,
        "p:dp:0"
    );
}

// Protects E/P/D triplet projection and prefill-only KV event ownership.
#[test]
fn epd_snapshot_projects_one_static_triplet_and_prefill_kv_source() {
    let build = BackendRegistryBuild::from_snapshot(epd_snapshot()).unwrap();
    let routes = build.registry.model_routes().routes();
    assert_eq!(routes.len(), 3);
    assert!(
        routes
            .iter()
            .any(|route| route.role == ModelServerRole::Encoder)
    );
    assert!(
        routes.iter().all(|route| {
            route.admission_targets.targets() == std::slice::from_ref(&route.target)
        })
    );
    assert_eq!(build.kv_runtime_config.route_bindings.len(), 1);
    assert_eq!(build.kv_runtime_config.event_sources.len(), 1);
    assert!(build.kv_runtime_config.route_bindings.contains_key("p"));
}

// Protects atomic route withdrawal when the controller publishes an empty snapshot.
#[test]
fn empty_snapshot_withdraws_all_routes() {
    let build = BackendRegistryBuild::from_snapshot(ServingSnapshot {
        version: 2,
        models: vec![],
        groups: vec![],
        pd_components: vec![],
        pd_pipeline_scopes: vec![],
        epd_components: vec![],
        epd_pipeline_scopes: vec![],
    })
    .unwrap();

    assert!(build.registry.model_routes().routes().is_empty());
    assert!(build.registry.configured_models().is_empty());
}

// Protects snapshot rejection for incomplete ownership and cross-component pipeline identities.
#[test]
fn invalid_scaling_identity_or_pipeline_scope_is_rejected() {
    let mut pd = pd_snapshot();
    pd.pd_pipeline_scopes[0].decode_route_target_ids.clear();
    assert!(matches!(
        BackendRegistry::from_snapshot(pd),
        Err(SnapshotError::InvalidPdPipelineScope(_))
    ));

    let mut epd = epd_snapshot();
    epd.epd_pipeline_scopes[0].decode_route_target_id = RouteTargetId::new("other");
    assert!(matches!(
        BackendRegistry::from_snapshot(epd),
        Err(SnapshotError::InvalidEpdPipelineScope(_))
    ));

    let mut aggregate = aggregate_snapshot("http://127.0.0.1:1".into());
    aggregate.groups[0].service_uid.clear();
    assert!(matches!(
        BackendRegistry::from_snapshot(aggregate),
        Err(SnapshotError::IncompleteGroup(_))
    ));

    let mut pd = pd_snapshot();
    pd.pd_components[0].pool_uid.clear();
    assert!(matches!(
        BackendRegistry::from_snapshot(pd),
        Err(SnapshotError::IncompletePdComponent(_))
    ));

    let mut epd = epd_snapshot();
    epd.epd_components[0].pool_name.clear();
    assert!(matches!(
        BackendRegistry::from_snapshot(epd),
        Err(SnapshotError::IncompleteEpdComponent(_))
    ));
}

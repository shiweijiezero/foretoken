use super::*;
use axum::Json;
use axum::Router;
use axum::http::StatusCode;
use axum::routing::get;
use foretoken_model_protocol::{
    RuntimeEcTransferMetadata, RuntimeMetadataResponse, RuntimeModelIdentity, TelemetryResponse,
    VLLM_SOURCE_REVISION,
};
use tokio::net::TcpListener;
use vllm_engine_core_client::protocol::dtype::ModelDtype;

use crate::{
    BackendRegistryBuild, SnapshotEpdComponent, SnapshotEpdDomain, SnapshotGroup,
    SnapshotPdComponent, SnapshotPdDomain,
};
use foretoken_model_protocol::{ModelServerRole, RouteStage};
use foretoken_router::RouteDecision;

fn component(id: &str, role: ModelServerRole) -> SnapshotPdComponent {
    SnapshotPdComponent {
        service_uid: "service".into(),
        pool_uid: "pool".into(),
        pool_name: "pool".into(),
        route_target_id: RouteTargetId::new(id),
        role,
        domain_id: "service-a".into(),
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
        endpoint: "http://127.0.0.1:9000".into(),
        prefill_bootstrap_endpoint: (role == ModelServerRole::Prefill)
            .then(|| "http://127.0.0.1:29001".into()),
        kv_scope_id: "scope".into(),
        data_parallel_size: 1,
    }
}
fn pd_snapshot() -> ServingSnapshot {
    ServingSnapshot {
        version: 1,
        models: vec![],
        groups: vec![],
        pd_components: vec![
            component("p", ModelServerRole::Prefill),
            component("d", ModelServerRole::Decode),
        ],
        pd_domains: vec![SnapshotPdDomain {
            domain_id: "service-a".into(),
            prefill_route_target_ids: vec![RouteTargetId::new("p")],
            decode_route_target_ids: vec![RouteTargetId::new("d")],
        }],
        epd_components: vec![],
        epd_domains: vec![],
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
        domain_id: "epd-a".into(),
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
        ec_runtime_fingerprint: if ec {
            "fingerprint".into()
        } else {
            String::new()
        },
        capabilities: ["chat".into()].into_iter().collect(),
        max_input_tokens: None,
        endpoint: "http://127.0.0.1:9000".into(),
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
        pd_domains: vec![],
        epd_components: vec![
            epd_component("e", ModelServerRole::Encoder),
            epd_component("p", ModelServerRole::Prefill),
            epd_component("d", ModelServerRole::Decode),
        ],
        epd_domains: vec![SnapshotEpdDomain {
            domain_id: "epd-a".into(),
            encoder_route_target_id: RouteTargetId::new("e"),
            prefill_route_target_id: RouteTargetId::new("p"),
            decode_route_target_id: RouteTargetId::new("d"),
        }],
    }
}

fn runtime_metadata(model: &str, revision: &str) -> RuntimeMetadataResponse {
    RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: model.into(),
            revision: revision.into(),
        },
        model_dtype: ModelDtype::BFloat16,
        effective_max_model_len: 32_768,
        vllm_source_revision: VLLM_SOURCE_REVISION.into(),
        vllm_version: "0.0.0".into(),
        ec_transfer: None,
        capabilities: ["chat".into()].into_iter().collect(),
    }
}

async fn serve_model_server(metadata: RuntimeMetadataResponse) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let metadata_route = metadata.clone();
    let app = Router::new()
        .route("/readyz", get(|| async { StatusCode::OK }))
        .route(
            "/v1/internal/metadata",
            get(move || {
                let metadata = metadata_route.clone();
                async move { Json(metadata) }
            }),
        )
        .route(
            "/v1/internal/telemetry",
            get(|| async {
                Json(TelemetryResponse {
                    version: 2,
                    collected_at_unix_ms: 1,
                    accepting: true,
                    running_requests: 0,
                    max_concurrent_requests: 1,
                    scheduler_running_requests: Some(0),
                    scheduler_waiting_requests: Some(0),
                    kv_cache_usage: Some(0.0),
                    prompt_tokens_total: Some(0),
                    generation_tokens_total: Some(0),
                    ttft_seconds: Default::default(),
                    tpot_seconds: Default::default(),
                    e2e_seconds: Default::default(),
                })
            }),
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
        pd_domains: vec![],
        epd_components: vec![],
        epd_domains: vec![],
    }
}

#[tokio::test]
async fn readiness_preserves_frontend_owned_capabilities() {
    let endpoint = serve_model_server(runtime_metadata("model", "r1")).await;
    let registry = BackendRegistry::from_snapshot(aggregate_snapshot(endpoint)).unwrap();

    registry.refresh_backend_readiness().await;

    assert!(registry.is_route_target_healthy(&RouteTargetId::new("a")));
    assert_eq!(
        registry.metadata(&RouteTargetId::new("a")),
        Some(runtime_metadata("model", "r1"))
    );
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

#[tokio::test]
async fn readiness_rejects_metadata_with_a_different_snapshot_identity() {
    let endpoint = serve_model_server(runtime_metadata("other-model", "r1")).await;
    let registry = BackendRegistry::from_snapshot(aggregate_snapshot(endpoint)).unwrap();

    registry.refresh_backend_readiness().await;

    assert!(!registry.is_route_target_healthy(&RouteTargetId::new("a")));
    assert!(registry.metadata(&RouteTargetId::new("a")).is_none());
}

#[tokio::test]
async fn readiness_rejects_metadata_with_a_different_source_revision() {
    let mut metadata = runtime_metadata("model", "r1");
    metadata.vllm_source_revision = "different-source".into();
    let endpoint = serve_model_server(metadata).await;
    let registry = BackendRegistry::from_snapshot(aggregate_snapshot(endpoint)).unwrap();

    registry.refresh_backend_readiness().await;

    assert!(!registry.is_route_target_healthy(&RouteTargetId::new("a")));
    assert!(registry.metadata(&RouteTargetId::new("a")).is_none());
}

#[test]
fn epd_runtime_metadata_must_match_the_controller_owned_ec_contract() {
    let expected = RuntimeExpectation {
        model: "model".into(),
        revision: "r1".into(),
        ec_transfer: Some(RuntimeEcTransferMetadata {
            role: "ec_producer".into(),
            profile: "ec-profile".into(),
            connector: "ECExampleConnector".into(),
            fingerprint: "fingerprint".into(),
        }),
    };
    let mut metadata = runtime_metadata("model", "r1");
    metadata.ec_transfer = expected.ec_transfer.clone();
    assert!(metadata_matches(&expected, &metadata));

    metadata.ec_transfer.as_mut().unwrap().fingerprint = "other".into();
    assert!(!metadata_matches(&expected, &metadata));
}

#[test]
fn builds_component_inventory_without_cartesian_links() {
    let registry = BackendRegistry::from_snapshot(pd_snapshot()).unwrap();
    assert_eq!(registry.route_table().routes().len(), 2);
    assert!(
        registry
            .route_table()
            .routes()
            .iter()
            .any(|route| route.role == ModelServerRole::Prefill)
    );
    assert!(
        registry
            .route_table()
            .routes()
            .iter()
            .any(|route| route.role == ModelServerRole::Decode)
    );
}

#[test]
fn preserves_pd_input_limits_in_route_inventory() {
    let mut snapshot = pd_snapshot();
    snapshot.pd_components[0].max_input_tokens = Some(4096);
    snapshot.pd_components[1].max_input_tokens = Some(8192);

    let registry = BackendRegistry::from_snapshot(snapshot).unwrap();
    let routes = registry.route_table().routes();

    assert_eq!(
        routes
            .iter()
            .find(|route| route.role == ModelServerRole::Prefill)
            .unwrap()
            .max_input_tokens,
        Some(4096)
    );
    assert_eq!(
        routes
            .iter()
            .find(|route| route.role == ModelServerRole::Decode)
            .unwrap()
            .max_input_tokens,
        Some(8192)
    );
}

#[test]
fn rejects_component_not_in_its_service_domain() {
    let mut snapshot = pd_snapshot();
    snapshot.pd_domains[0].decode_route_target_ids.clear();
    assert!(matches!(
        BackendRegistry::from_snapshot(snapshot),
        Err(SnapshotError::InvalidPdDomain(_))
    ));
}
#[test]
fn resolver_resolves_only_the_router_selected_stage() {
    let registry = BackendRegistry::from_snapshot(pd_snapshot()).unwrap();
    let prefill = RouteDecision {
        route_target_id: RouteTargetId::new("p"),
        role: ModelServerRole::Prefill,
        model: "model".into(),
        revision: "r1".into(),
        data_parallel_rank: 0,
    };
    assert!(
        <BackendRegistry as foretoken_llm_facade::LlmFacadeResolver>::resolve_stage(
            &registry,
            &prefill,
            RouteStage::Prefill,
        )
        .is_some()
    );
    assert!(
        <BackendRegistry as foretoken_llm_facade::LlmFacadeResolver>::resolve_stage(
            &registry,
            &prefill,
            RouteStage::Decode,
        )
        .is_none()
    );
}

#[test]
fn readiness_requires_a_healthy_pd_pair() {
    let registry = BackendRegistry::from_snapshot(pd_snapshot()).unwrap();
    registry.health[&RouteTargetId::new("p")].store(true, Ordering::Release);
    assert!(!registry.is_ready());
    assert!(registry.healthy_models().is_empty());

    registry.health[&RouteTargetId::new("d")].store(true, Ordering::Release);
    assert!(registry.is_ready());
    assert_eq!(registry.healthy_models(), vec!["model"]);
}

#[test]
fn epd_snapshot_requires_one_compatible_static_triplet() {
    let registry = BackendRegistry::from_snapshot(epd_snapshot()).unwrap();
    assert_eq!(registry.route_table().routes().len(), 3);
    assert!(
        registry
            .route_table()
            .routes()
            .iter()
            .any(|route| route.role == ModelServerRole::Encoder)
    );
    let target_set = registry.logical_target_set("model").unwrap();
    assert!(
        registry
            .route_table()
            .routes()
            .iter()
            .all(|route| target_set.targets() == std::slice::from_ref(&route.target))
    );

    let mut incompatible = epd_snapshot();
    incompatible.epd_components[1].ec_runtime_fingerprint = "other".into();
    assert!(matches!(
        BackendRegistry::from_snapshot(incompatible),
        Err(SnapshotError::InvalidEpdDomain(_))
    ));

    let mut not_a_triplet = epd_snapshot();
    not_a_triplet.epd_domains[0].decode_route_target_id = RouteTargetId::new("other");
    assert!(matches!(
        BackendRegistry::from_snapshot(not_a_triplet),
        Err(SnapshotError::InvalidEpdDomain(_))
    ));
}

#[test]
fn epd_prefill_component_is_included_in_the_kv_prefix_index() {
    let build = BackendRegistryBuild::from_snapshot(epd_snapshot()).unwrap();
    assert_eq!(build.kv_runtime_config.route_bindings.len(), 1);
    assert_eq!(build.kv_runtime_config.event_sources.len(), 1);
    assert!(build.kv_runtime_config.route_bindings.contains_key("p"));
}

#[test]
fn kv_index_binds_and_polls_only_prefill_components() {
    let build = BackendRegistryBuild::from_snapshot(pd_snapshot()).unwrap();
    assert_eq!(build.kv_runtime_config.route_bindings.len(), 1);
    assert_eq!(build.kv_runtime_config.event_sources.len(), 1);
    assert!(build.kv_runtime_config.route_bindings.contains_key("p"));
    assert_eq!(
        build.kv_runtime_config.event_sources[0].event_source_id,
        "p:dp:0"
    );
    assert_eq!(build.kv_runtime_config.event_sources[0].model_group_id, "p");
    assert_eq!(build.kv_runtime_config.event_sources[0].dp_rank, 0);
}

#[test]
fn empty_scaling_identity_is_rejected() {
    let mut aggregate = aggregate_snapshot("http://127.0.0.1:8000".into());
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

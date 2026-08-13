use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use bytes::Bytes;
use foretoken_model_protocol::{
    CumulativeHistogram, CumulativeHistogramBucket, RuntimeMetadataResponse, RuntimeModelIdentity,
};
use futures::stream;
use http_body_util::BodyExt;
use tower::ServiceExt;
use vllm_engine_core_client::protocol::dtype::ModelDtype;
use vllm_engine_core_client::protocol::multimodal::{
    MmBatchedField, MmFeatureSpec, MmField, MmFieldElem, MmKwargValue, PlaceholderRange,
};
use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
use vllm_engine_core_client::protocol::tensor::WireNdArray;

use super::{AppState, RuntimeHealth, router};
use crate::backend::{
    Backend, BackendError, BackendTelemetry, GenerateInput, TokenEvent, TokenOutput, TokenStream,
};

struct RecordingBackend {
    requests: Mutex<Vec<GenerateInput>>,
    aborts: Mutex<Vec<Vec<String>>>,
    telemetry: BackendTelemetry,
}

impl Default for RecordingBackend {
    fn default() -> Self {
        Self {
            requests: Mutex::new(Vec::new()),
            aborts: Mutex::new(Vec::new()),
            telemetry: BackendTelemetry {
                running_requests: 0,
                max_concurrent_requests: 7,
                ..Default::default()
            },
        }
    }
}

#[async_trait]
impl Backend for RecordingBackend {
    async fn generate(&self, input: GenerateInput) -> Result<TokenStream, BackendError> {
        self.requests.lock().unwrap().push(input.clone());
        Ok(Box::pin(stream::iter([Ok(TokenEvent::Token(Box::new(
            TokenOutput {
                request_id: input.request_id,
                prompt_token_ids: Some(input.prompt_token_ids),
                prompt_logprobs: None,
                token_ids: vec![42],
                logprobs: None,
                cached_token_count: 2,
                finish_reason: Some(vllm_llm::FinishReason::stop_eos()),
                kv_transfer_params: None,
                ec_transfer_params: None,
            },
        )))])))
    }

    async fn abort(&self, request_ids: &[String]) -> Result<(), BackendError> {
        self.aborts.lock().unwrap().push(request_ids.to_vec());
        Ok(())
    }

    fn telemetry(&self) -> BackendTelemetry {
        self.telemetry.clone()
    }
}

fn metadata() -> RuntimeMetadataResponse {
    RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: "model".into(),
            revision: "r1".into(),
        },
        model_dtype: ModelDtype::BFloat16,
        effective_max_model_len: 32_768,
        vllm_version: "0.0.0".into(),
        ec_transfer: None,
        capabilities: Default::default(),
    }
}

fn app(backend: Arc<dyn Backend>, healthy: bool, accepting: bool) -> axum::Router {
    app_with_body_limit(backend, healthy, accepting, 64 * 1024 * 1024)
}

fn app_with_body_limit(
    backend: Arc<dyn Backend>,
    healthy: bool,
    accepting: bool,
    body_limit: usize,
) -> axum::Router {
    let health = Arc::new(RuntimeHealth::new());
    health.set_process_alive(healthy);
    health.set_client_healthy(healthy);
    health.set_accepting(accepting);
    router(AppState::new(backend, health, metadata()), body_limit)
}

fn assert_default_telemetry_snapshot(body: Bytes, running_requests: u64) {
    let telemetry: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(telemetry["version"], 2);
    assert!(telemetry["collected_at_unix_ms"].as_u64().is_some());
    assert_eq!(telemetry["accepting"], false);
    assert_eq!(telemetry["running_requests"], running_requests);
    assert_eq!(telemetry["max_concurrent_requests"], 7);
    assert_eq!(
        telemetry["scheduler_running_requests"],
        serde_json::Value::Null
    );
    assert_eq!(
        telemetry["scheduler_waiting_requests"],
        serde_json::Value::Null
    );
    assert_eq!(telemetry["kv_cache_usage"], serde_json::Value::Null);
    assert_eq!(telemetry["prompt_tokens_total"], serde_json::Value::Null);
    assert_eq!(
        telemetry["generation_tokens_total"],
        serde_json::Value::Null
    );
}

#[tokio::test]
async fn missing_kv_adapter_disables_only_the_soft_hint_endpoint() {
    let app = app(Arc::new(RecordingBackend::default()), true, true);
    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/internal/kv-index/delta?dpRank=0")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
}

struct PendingStreamBackend;

#[async_trait]
impl Backend for PendingStreamBackend {
    #[allow(unused_variables)]
    async fn generate(&self, request: GenerateInput) -> Result<TokenStream, BackendError> {
        Ok(Box::pin(stream::pending()))
    }

    #[allow(unused_variables)]
    async fn abort(&self, request_ids: &[String]) -> Result<(), BackendError> {
        Ok(())
    }

    fn telemetry(&self) -> BackendTelemetry {
        BackendTelemetry {
            running_requests: 0,
            max_concurrent_requests: 7,
            ..Default::default()
        }
    }
}

struct FailingStreamBackend;

#[async_trait]
impl Backend for FailingStreamBackend {
    #[allow(unused_variables)]
    async fn generate(&self, request: GenerateInput) -> Result<TokenStream, BackendError> {
        Ok(Box::pin(stream::iter([Err(BackendError::Unavailable)])))
    }

    #[allow(unused_variables)]
    async fn abort(&self, request_ids: &[String]) -> Result<(), BackendError> {
        Ok(())
    }

    fn telemetry(&self) -> BackendTelemetry {
        BackendTelemetry {
            running_requests: 0,
            max_concurrent_requests: 0,
            ..Default::default()
        }
    }
}

#[tokio::test]
async fn generate_forwards_pretokenized_input_as_ndjson() {
    let backend = Arc::new(RecordingBackend::default());
    let body = serde_json::json!({
        "request_id": "request-1",
        "prompt_token_ids": [1, 2],
        "sampling_params": EngineCoreSamplingParams::default(),
        "priority": -2
    });
    let response = app(backend.clone(), true, true)
        .oneshot(
            Request::post("/v1/internal/generate")
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), 200);
    assert_eq!(
        response.headers().get("content-type").unwrap(),
        "application/x-ndjson"
    );
    assert_eq!(
        response.into_body().collect().await.unwrap().to_bytes(),
        "{\"type\":\"token\",\"request_id\":\"request-1\",\"prompt_token_ids\":[1,2],\"prompt_logprobs\":null,\"token_ids\":[42],\"logprobs\":null,\"cached_token_count\":2,\"finish_reason\":{\"Stop\":null},\"kv_transfer_params\":null,\"ec_transfer_params\":null}\n"
    );
    assert_eq!(backend.requests.lock().unwrap()[0].prompt_token_ids, [1, 2]);
    assert_eq!(backend.requests.lock().unwrap()[0].priority, -2);
}

#[tokio::test]
async fn generate_accepts_msgpack_multimodal_tensors() {
    let backend = Arc::new(RecordingBackend::default());
    let tensor = WireNdArray::from_f32(vec![2], vec![1.0, 2.0]).unwrap();
    let mut data = BTreeMap::new();
    data.insert(
        "pixel_values".into(),
        MmFieldElem {
            data: Some(MmKwargValue::Tensor(tensor)),
            field: MmField::Batched(MmBatchedField { keep_on_cpu: false }),
        },
    );
    let body = rmp_serde::to_vec_named(&foretoken_model_protocol::GenerateInput {
        stage: foretoken_model_protocol::RouteStage::Aggregate,
        request_id: "request-mm".into(),
        prompt_token_ids: vec![1, 2],
        mm_features: Some(vec![MmFeatureSpec {
            data: Some(data),
            modality: "image".into(),
            identifier: "image-1".into(),
            mm_position: PlaceholderRange {
                offset: 1,
                length: 1,
                is_embed: None,
            },
            mm_hash: None,
        }]),
        sampling_params: EngineCoreSamplingParams::default(),
        arrival_time: None,
        cache_salt: None,
        trace_headers: None,
        priority: 0,
        data_parallel_rank: None,
        session_id: Some("session-mm".into()),
        reasoning_parser_kwargs: None,
        lora_request: None,
    })
    .unwrap();

    let response = app(backend.clone(), true, true)
        .oneshot(
            Request::post("/v1/internal/generate")
                .header("content-type", "application/msgpack")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    let requests = backend.requests.lock().unwrap();
    assert_eq!(requests[0].session_id.as_deref(), Some("session-mm"));
    assert_eq!(requests[0].mm_features.as_ref().unwrap().len(), 1);
}

#[tokio::test]
async fn stream_errors_are_encoded_as_one_typed_terminal_event() {
    let response = app(Arc::new(FailingStreamBackend), true, true)
        .oneshot(
            Request::post("/v1/internal/generate")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"request_id":"request-1","prompt_token_ids":[1],"sampling_params":{}}"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), 200);
    assert_eq!(
        response.into_body().collect().await.unwrap().to_bytes(),
        r#"{"type":"error","request_id":"request-1","code":"unavailable"}
"#,
    );
}

#[tokio::test]
async fn abort_forwards_only_the_explicit_request_ids() {
    let backend = Arc::new(RecordingBackend::default());
    let response = app(backend.clone(), true, true)
        .oneshot(
            Request::post("/v1/internal/abort")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"request_ids":["request-1"]}"#))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), 204);
    assert_eq!(backend.aborts.lock().unwrap().as_slice(), [["request-1"]]);
}

#[tokio::test]
async fn abort_rejects_an_unscoped_empty_request() {
    let backend = Arc::new(RecordingBackend::default());
    let response = app(backend.clone(), true, true)
        .oneshot(
            Request::post("/v1/internal/abort")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"request_ids":[]}"#))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), 400);
    assert!(backend.aborts.lock().unwrap().is_empty());
}

#[tokio::test]
async fn internal_requests_reject_unknown_fields() {
    let backend = Arc::new(RecordingBackend::default());
    for (path, body) in [
        (
            "/v1/internal/generate",
            r#"{"request_id":"r","prompt_token_ids":[1],"sampling_params":{},"unsupported":true}"#,
        ),
        (
            "/v1/internal/abort",
            r#"{"request_ids":["r"],"unsupported":true}"#,
        ),
    ] {
        let response = app(backend.clone(), true, true)
            .oneshot(
                Request::post(path)
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert!(
            response.status().is_client_error(),
            "{path} returned {}",
            response.status()
        );
    }
    assert!(backend.requests.lock().unwrap().is_empty());
    assert!(backend.aborts.lock().unwrap().is_empty());
}

#[tokio::test]
async fn metadata_exposes_observed_enginecore_values_without_feature_claims() {
    let response = app(Arc::new(RecordingBackend::default()), true, true)
        .oneshot(
            Request::get("/v1/internal/metadata")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.into_body().collect().await.unwrap().to_bytes(),
        r#"{"version":1,"model":{"model":"model","revision":"r1"},"model_dtype":"bfloat16","effective_max_model_len":32768,"vllm_version":"0.0.0","ec_transfer":null,"capabilities":[]}"#,
    );
}

#[tokio::test]
async fn telemetry_exposes_cumulative_backend_snapshot() {
    let histogram = CumulativeHistogram {
        count: 2,
        sum_seconds: 0.3,
        buckets: vec![CumulativeHistogramBucket {
            le_seconds: 0.5,
            count: 2,
        }],
    };
    let backend = Arc::new(RecordingBackend {
        telemetry: BackendTelemetry {
            running_requests: 3,
            max_concurrent_requests: 7,
            scheduler_running_requests: Some(2),
            scheduler_waiting_requests: Some(1),
            kv_cache_usage: Some(0.75),
            prompt_tokens_total: Some(12),
            generation_tokens_total: Some(8),
            ttft_seconds: histogram.clone(),
            tpot_seconds: histogram.clone(),
            e2e_seconds: histogram,
        },
        ..Default::default()
    });
    let response = app(backend, true, false)
        .oneshot(
            Request::get("/v1/internal/telemetry")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), 200);
    let telemetry: serde_json::Value =
        serde_json::from_slice(&response.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(telemetry["version"], 2);
    assert!(telemetry["collected_at_unix_ms"].as_u64().is_some());
    assert_eq!(telemetry["accepting"], false);
    assert_eq!(telemetry["running_requests"], 3);
    assert_eq!(telemetry["max_concurrent_requests"], 7);
    assert_eq!(telemetry["scheduler_running_requests"], 2);
    assert_eq!(telemetry["scheduler_waiting_requests"], 1);
    assert_eq!(telemetry["kv_cache_usage"], 0.75);
    assert_eq!(telemetry["prompt_tokens_total"], 12);
    assert_eq!(telemetry["generation_tokens_total"], 8);
    assert_eq!(
        telemetry["ttft_seconds"],
        serde_json::json!({
            "count": 2,
            "sum_seconds": 0.3,
            "buckets": [{"le_seconds": 0.5, "count": 2}]
        })
    );
    assert_eq!(telemetry["tpot_seconds"], telemetry["ttft_seconds"]);
    assert_eq!(telemetry["e2e_seconds"], telemetry["ttft_seconds"]);
}

#[tokio::test]
async fn readiness_gates_generation_without_calling_the_backend() {
    let backend = Arc::new(RecordingBackend::default());
    let response = app(backend.clone(), false, false)
        .oneshot(
            Request::post("/v1/internal/generate")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"request_id":"r","prompt_token_ids":[1],"sampling_params":{}}"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), 503);
    assert!(backend.requests.lock().unwrap().is_empty());
}

#[tokio::test]
async fn generate_accepts_processed_multimodal_bodies_above_axum_default() {
    let response = app(Arc::new(RecordingBackend::default()), true, true)
        .oneshot(
            Request::post("/v1/internal/generate")
                .header("content-type", "application/json")
                .body(Body::from(vec![b' '; 3 * 1024 * 1024]))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn generate_enforces_the_configured_request_body_limit() {
    let response = app_with_body_limit(
        Arc::new(RecordingBackend::default()),
        true,
        true,
        1024 * 1024,
    )
    .oneshot(
        Request::post("/v1/internal/generate")
            .header("content-type", "application/json")
            .body(Body::from(vec![b' '; 2 * 1024 * 1024]))
            .unwrap(),
    )
    .await
    .unwrap();

    assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
}

#[tokio::test]
async fn health_stays_live_while_draining_but_readiness_and_admission_close() {
    let backend = Arc::new(RecordingBackend::default());
    let app = app(backend.clone(), true, false);
    let health = app
        .clone()
        .oneshot(Request::get("/healthz").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let ready = app
        .clone()
        .oneshot(Request::get("/readyz").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let generate = app
        .oneshot(
            Request::post("/v1/internal/generate")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"request_id":"r","prompt_token_ids":[1],"sampling_params":{}}"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(health.status(), 200);
    assert_eq!(ready.status(), 503);
    assert_eq!(generate.status(), 503);
    assert!(backend.requests.lock().unwrap().is_empty());
}

#[tokio::test]
async fn admission_close_observes_every_accepted_response_stream() {
    let app = app(Arc::new(PendingStreamBackend), true, true);
    let response = app
        .clone()
        .oneshot(
            Request::post("/v1/internal/generate")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"request_id":"r","prompt_token_ids":[1],"sampling_params":{}}"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let close = app
        .clone()
        .oneshot(
            Request::post("/v1/internal/admission/close")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_default_telemetry_snapshot(close.into_body().collect().await.unwrap().to_bytes(), 1);

    drop(response);
    let close = app
        .oneshot(
            Request::post("/v1/internal/admission/close")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_default_telemetry_snapshot(close.into_body().collect().await.unwrap().to_bytes(), 0);
}

#[tokio::test]
async fn admission_close_is_idempotent_and_keeps_abort_available() {
    let backend = Arc::new(RecordingBackend::default());
    let app = app(backend.clone(), true, true);

    for _ in 0..2 {
        let close = app
            .clone()
            .oneshot(
                Request::post("/v1/internal/admission/close")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(close.status(), StatusCode::OK);
        assert_default_telemetry_snapshot(close.into_body().collect().await.unwrap().to_bytes(), 0);
    }

    let abort = app
        .clone()
        .oneshot(
            Request::post("/v1/internal/abort")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"request_ids":["request-1"]}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(abort.status(), StatusCode::NO_CONTENT);
    assert_eq!(backend.aborts.lock().unwrap().as_slice(), [["request-1"]]);

    let generate = app
        .oneshot(
            Request::post("/v1/internal/generate")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"request_id":"r","prompt_token_ids":[1],"sampling_params":{}}"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(generate.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(backend.requests.lock().unwrap().is_empty());
}

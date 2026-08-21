// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use foretoken_model_protocol::{
    CumulativeHistogram, CumulativeHistogramBucket, RuntimeMetadataResponse, RuntimeModelIdentity,
};
use foretoken_model_server::api::{AppState, RuntimeHealth, router};
use foretoken_model_server::backend::{
    Backend, BackendError, BackendTelemetry, GenerateInput, TokenEvent, TokenOutput, TokenStream,
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

#[tokio::test]
async fn kv_delta_is_unavailable_without_an_event_adapter() {
    let response = app(Arc::new(RecordingBackend::default()), true, true)
        .oneshot(
            Request::get("/v1/internal/kv-index/delta?dpRank=0")
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
    async fn generate(&self, _: GenerateInput) -> Result<TokenStream, BackendError> {
        Ok(Box::pin(stream::pending()))
    }

    async fn abort(&self, _: &[String]) -> Result<(), BackendError> {
        Ok(())
    }

    fn telemetry(&self) -> BackendTelemetry {
        BackendTelemetry {
            max_concurrent_requests: 7,
            ..Default::default()
        }
    }
}

struct FailingStreamBackend;

#[async_trait]
impl Backend for FailingStreamBackend {
    async fn generate(&self, _: GenerateInput) -> Result<TokenStream, BackendError> {
        Ok(Box::pin(stream::iter([Err(BackendError::Unavailable)])))
    }

    async fn abort(&self, _: &[String]) -> Result<(), BackendError> {
        Ok(())
    }

    fn telemetry(&self) -> BackendTelemetry {
        Default::default()
    }
}

#[tokio::test]
async fn generate_forwards_json_input_and_encodes_ndjson() {
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

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.headers().get("content-type").unwrap(),
        "application/x-ndjson"
    );
    assert_eq!(
        response.into_body().collect().await.unwrap().to_bytes(),
        "{\"type\":\"token\",\"request_id\":\"request-1\",\"prompt_token_ids\":[1,2],\"prompt_logprobs\":null,\"token_ids\":[42],\"logprobs\":null,\"cached_token_count\":2,\"finish_reason\":{\"Stop\":null},\"kv_transfer_params\":null,\"ec_transfer_params\":null}\n"
    );
    let requests = backend.requests.lock().unwrap();
    assert_eq!(requests[0].prompt_token_ids, [1, 2]);
    assert_eq!(requests[0].priority, -2);
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

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.into_body().collect().await.unwrap().to_bytes(),
        r#"{"type":"error","request_id":"request-1","code":"unavailable"}
"#,
    );
}

#[tokio::test]
async fn abort_requires_scoped_request_ids_and_forwards_them() {
    let backend = Arc::new(RecordingBackend::default());
    let app = app(backend.clone(), true, true);

    let empty = app
        .clone()
        .oneshot(
            Request::post("/v1/internal/abort")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"request_ids":[]}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(empty.status(), StatusCode::BAD_REQUEST);

    let response = app
        .oneshot(
            Request::post("/v1/internal/abort")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"request_ids":["request-1"]}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NO_CONTENT);
    assert_eq!(backend.aborts.lock().unwrap().as_slice(), [["request-1"]]);
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
async fn metadata_and_telemetry_expose_typed_runtime_snapshots() {
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
    let app = app(backend, true, false);

    let metadata = app
        .clone()
        .oneshot(
            Request::get("/v1/internal/metadata")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(metadata.status(), StatusCode::OK);
    assert_eq!(
        metadata.into_body().collect().await.unwrap().to_bytes(),
        r#"{"version":1,"model":{"model":"model","revision":"r1"},"model_dtype":"bfloat16","effective_max_model_len":32768,"ec_transfer":null,"capabilities":[]}"#,
    );

    let telemetry = app
        .oneshot(
            Request::get("/v1/internal/telemetry")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(telemetry.status(), StatusCode::OK);
    let telemetry: serde_json::Value =
        serde_json::from_slice(&telemetry.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(telemetry["version"], 2);
    assert!(telemetry["collected_at_unix_ms"].as_u64().is_some());
    assert_eq!(telemetry["accepting"], false);
    assert_eq!(telemetry["running_requests"], 3);
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
async fn readiness_and_admission_gate_generation_without_backend_calls() {
    let backend = Arc::new(RecordingBackend::default());
    let unavailable = app(backend.clone(), false, false)
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
    assert_eq!(unavailable.status(), StatusCode::SERVICE_UNAVAILABLE);

    let draining = app(backend.clone(), true, false);
    let health = draining
        .clone()
        .oneshot(Request::get("/healthz").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let ready = draining
        .clone()
        .oneshot(Request::get("/readyz").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let generate = draining
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
    assert_eq!(health.status(), StatusCode::OK);
    assert_eq!(ready.status(), StatusCode::OK);
    assert_eq!(generate.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(backend.requests.lock().unwrap().is_empty());
}

#[tokio::test]
async fn configured_body_limit_is_enforced_after_the_default_axum_limit_is_overridden() {
    let accepts_large_request = app(Arc::new(RecordingBackend::default()), true, true)
        .oneshot(
            Request::post("/v1/internal/generate")
                .header("content-type", "application/json")
                .body(Body::from(vec![b' '; 3 * 1024 * 1024]))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(accepts_large_request.status(), StatusCode::BAD_REQUEST);

    let rejects_oversized_request = app_with_body_limit(
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
    assert_eq!(
        rejects_oversized_request.status(),
        StatusCode::PAYLOAD_TOO_LARGE
    );
}

#[tokio::test]
async fn admission_close_tracks_open_streams() {
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
    let telemetry: serde_json::Value =
        serde_json::from_slice(&close.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(telemetry["accepting"], false);
    assert_eq!(telemetry["running_requests"], 1);
    let ready = app
        .clone()
        .oneshot(Request::get("/readyz").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(ready.status(), StatusCode::OK);

    drop(response);
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
    let telemetry: serde_json::Value =
        serde_json::from_slice(&close.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(telemetry["running_requests"], 0);

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
}

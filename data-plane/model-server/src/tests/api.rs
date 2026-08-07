use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use foretoken_model_protocol::{
    RuntimeMetadataResponse, RuntimeModelIdentity, VLLM_PINNED_REVISION,
};
use futures::stream;
use http_body_util::BodyExt;
use tower::ServiceExt;
use vllm_engine_core_client::protocol::dtype::ModelDtype;
use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;

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
        self.telemetry
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
        vllm_pinned_revision: VLLM_PINNED_REVISION.into(),
        vllm_version: "0.0.0".into(),
        ec_transfer: None,
        capabilities: Default::default(),
    }
}

fn app(backend: Arc<dyn Backend>, healthy: bool, accepting: bool) -> axum::Router {
    let health = Arc::new(RuntimeHealth::new());
    health.set_process_alive(healthy);
    health.set_client_healthy(healthy);
    health.set_accepting(accepting);
    router(AppState::new(backend, health, metadata()))
}

#[tokio::test]
async fn missing_kv_adapter_disables_only_the_soft_hint_endpoint() {
    let app = app(Arc::new(RecordingBackend::default()), true, true);
    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/internal/kv-index/delta")
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
            running_requests: 0,
            max_concurrent_requests: 7,
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
        BackendTelemetry {
            running_requests: 0,
            max_concurrent_requests: 0,
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
        r#"{"version":1,"model":{"model":"model","revision":"r1"},"model_dtype":"bfloat16","effective_max_model_len":32768,"vllm_pinned_revision":"5b14019576475224d86044b262e28a04a85d4086","vllm_version":"0.0.0","ec_transfer":null,"capabilities":[]}"#,
    );
}

#[tokio::test]
async fn telemetry_exposes_capacity_runtime_admission_and_backend_work() {
    let backend = Arc::new(RecordingBackend {
        telemetry: BackendTelemetry {
            running_requests: 3,
            max_concurrent_requests: 7,
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
    assert_eq!(
        response.into_body().collect().await.unwrap().to_bytes(),
        r#"{"version":1,"accepting":false,"running_requests":3,"max_concurrent_requests":7}"#,
    );
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
    assert_eq!(
        close.into_body().collect().await.unwrap().to_bytes(),
        r#"{"version":1,"accepting":false,"running_requests":1,"max_concurrent_requests":7}"#,
    );

    drop(response);
    let close = app
        .oneshot(
            Request::post("/v1/internal/admission/close")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        close.into_body().collect().await.unwrap().to_bytes(),
        r#"{"version":1,"accepting":false,"running_requests":0,"max_concurrent_requests":7}"#,
    );
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
        assert_eq!(
            close.into_body().collect().await.unwrap().to_bytes(),
            r#"{"version":1,"accepting":false,"running_requests":0,"max_concurrent_requests":7}"#,
        );
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

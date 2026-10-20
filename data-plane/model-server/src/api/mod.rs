// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Restricted internal HTTP routes for already-tokenized EngineCore requests.

use std::convert::Infallible;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::extract::rejection::JsonRejection;
use axum::extract::{DefaultBodyLimit, Query, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use bytes::Bytes;
use futures::StreamExt;
use prometheus_client::encoding::text::{encode_eof, encode_registry};
use prometheus_client::registry::Registry;
use serde::Serialize;

use crate::backend::vllm::kv_events::{KvDeltaError, KvEventAdapter};
use crate::backend::{Backend, BackendError, GenerateInput, TokenEvent};
use crate::execution::{
    AdmissionRejection, ExecutionError, ExecutionStore, InstanceAdmissionStatus, RuntimeHealth,
    start_execution,
};
use crate::runtime_cache;
use foretoken_model_protocol::{
    AbortInput, KV_INDEX_DELTA_PATH, KvDeltaQuery, RuntimeMetadataResponse, SchedulerLoadResponse,
    TelemetryResponse,
};

const OPENMETRICS_CONTENT_TYPE: &str = "application/openmetrics-text; version=1.0.0; charset=utf-8";

/// Mutable process state shared by typed HTTP handlers.
#[derive(Clone)]
pub struct AppState {
    backend: Arc<dyn Backend>,
    health: Arc<RuntimeHealth>,
    metadata: RuntimeMetadataResponse,
    kv_events: Option<Arc<KvEventAdapter>>,
    runtime_cache: Option<runtime_cache::Config>,
    profiling: Option<crate::profiling::Handle>,
    shared_kv: Option<crate::backend::vllm::prefix_lookup::SharedKvLookup>,
    ledger: Option<ExecutionStore>,
}

/// Fixed, wire-safe errors for the internal API.
#[derive(Debug, Clone, Copy)]
enum ApiError {
    InvalidRequest,
    Unavailable,
    Busy,
    AdmissionUnavailable,
    Rejected,
    Protocol,
    RequestFailed,
}

#[derive(Serialize)]
struct ErrorDetail {
    code: &'static str,
    message: &'static str,
}

#[derive(Serialize)]
struct ErrorBody {
    error: ErrorDetail,
}

/// Constructs the group-local API consumed by the Pod-local frontend, not an OpenAI router.
///
/// Server bootstrap moves `state` into the returned router, which owns it for all handler lifetimes.
pub fn router(state: AppState, internal_generate_request_body_limit_bytes: usize) -> Router {
    let profiling = crate::profiling::router(state.profiling.clone());
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/metrics", get(metrics))
        .route("/v1/internal/metadata", get(metadata))
        .route("/v1/internal/telemetry", get(telemetry))
        .route("/v1/internal/scheduler-load", get(scheduler_load))
        .route("/v1/internal/admission", get(instance_admission_status))
        .route(
            "/v1/internal/admission/ready",
            get(instance_admission_ready),
        )
        .route("/v1/internal/admission/close", post(close_admission))
        .route("/v1/internal/generate", post(generate))
        .route("/v1/internal/abort", post(abort))
        .route(KV_INDEX_DELTA_PATH, get(kv_index_delta))
        .route(
            foretoken_model_protocol::KV_LOCAL_PREFIX_PATH,
            post(local_kv_prefix),
        )
        .route(
            foretoken_model_protocol::KV_SHARED_PREFIX_PATH,
            post(shared_kv_prefix),
        )
        .with_state(state)
        .merge(profiling)
        .layer(DefaultBodyLimit::max(
            internal_generate_request_body_limit_bytes,
        ))
}

impl AppState {
    /// Builds state consumed by internal HTTP handlers; the server owns the supplied backend state.
    pub fn new(
        backend: Arc<dyn Backend>,
        health: Arc<RuntimeHealth>,
        metadata: RuntimeMetadataResponse,
    ) -> Self {
        Self {
            backend,
            health,
            metadata,
            kv_events: None,
            runtime_cache: None,
            profiling: None,
            shared_kv: None,
            ledger: None,
        }
    }

    /// Attaches the platform capacity ledger used to accept and finish frontend reservations.
    pub fn with_request_ledger(
        mut self,
        ledger: Arc<foretoken_request_ledger::RequestLedger>,
        pod_uid: String,
        epoch: u64,
    ) -> Self {
        self.ledger = Some(ExecutionStore {
            ledger,
            pod_uid,
            epoch,
        });
        self
    }

    /// Attaches the shared KV delta source used by the index endpoint and returns updated state.
    ///
    /// The router owns this state while its handlers retain cloned adapter references.
    pub fn with_kv_events(mut self, adapter: Arc<KvEventAdapter>) -> Self {
        self.kv_events = Some(adapter);
        self
    }

    /// Enables read-only shared-prefix observations through the running connector.
    pub fn with_shared_kv(
        mut self,
        lookup: crate::backend::vllm::prefix_lookup::SharedKvLookup,
    ) -> Self {
        self.shared_kv = Some(lookup);
        self
    }

    /// Attaches RuntimeCache filesystem telemetry rendered with backend metrics.
    pub fn with_runtime_cache(mut self, config: runtime_cache::Config) -> Self {
        self.runtime_cache = Some(config);
        self
    }

    /// Attaches diagnostic control on the existing internal listener without transferring supervision.
    pub fn with_profiling(mut self, handle: crate::profiling::Handle) -> Self {
        self.profiling = Some(handle);
        self
    }
}

/// Decodes an internal request and transfers its instance slot to the engine execution task.
async fn generate(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    // Multimodal tensors use MessagePack; ordinary requests keep a human-readable JSON boundary.
    let input: GenerateInput = if content_type_is(&headers, "application/msgpack") {
        rmp_serde::from_slice(&body).map_err(|_| ApiError::InvalidRequest)?
    } else {
        serde_json::from_slice(&body).map_err(|_| ApiError::InvalidRequest)?
    };
    if input
        .reservation
        .as_ref()
        .is_some_and(|reference| reference.model != state.metadata.model.model)
    {
        return Err(ApiError::InvalidRequest);
    }
    if !state.health.healthy() {
        return Err(ApiError::Unavailable);
    }
    let permit = state.health.try_admit().map_err(|error| match error {
        AdmissionRejection::Closed => ApiError::Unavailable,
        AdmissionRejection::Busy => ApiError::Busy,
    })?;
    let request_id = input.request_id.clone();
    let stream = start_execution(state.backend, state.health, state.ledger, input, permit)
        .await
        .map_err(|error| match error {
            ExecutionError::AdmissionUnavailable => ApiError::AdmissionUnavailable,
            ExecutionError::Backend(error) => ApiError::backend(error),
        })?;
    // Once headers are sent, backend failures become typed terminal events rather than a new HTTP status.
    let body_stream = stream.map(move |item| {
        let event = match item {
            Ok(event) => event,
            Err(error) => TokenEvent::Error {
                request_id: request_id.clone(),
                code: error.token_error_code(),
            },
        };
        let mut encoded = serde_json::to_vec(&event).expect("TokenEvent always serializes");
        encoded.push(b'\n');
        Ok::<Bytes, Infallible>(Bytes::from(encoded))
    });
    Ok((
        StatusCode::OK,
        [(header::CONTENT_TYPE, "application/x-ndjson")],
        Body::from_stream(body_stream),
    )
        .into_response())
}

/// Serves the workload probe only when engine health and an applied admission generation are present.
async fn instance_admission_ready(State(state): State<AppState>) -> StatusCode {
    if state.health.ready()
        && state
            .health
            .instance_admission_status()
            .active_generation
            .is_some()
    {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

/// Publishes applied limits and occupancy to the controller on the execution listener.
async fn instance_admission_status(State(state): State<AppState>) -> Json<InstanceAdmissionStatus> {
    Json(state.health.instance_admission_status())
}

/// Queries schedulers through their existing utility transport without admitting a generation.
async fn local_kv_prefix(
    State(state): State<AppState>,
    Json(request): Json<foretoken_model_protocol::KvPrefixRequest>,
) -> Response {
    if !state.health.ready() || !state.health.accepting() {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let Some(adapter) = state.kv_events else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    match tokio::time::timeout(
        foretoken_model_protocol::KV_OBSERVATION_TIMEOUT,
        state.backend.local_prefixes(&request),
    )
    .await
    {
        Ok(Ok(ranks)) => Json(adapter.prefix_response(ranks)).into_response(),
        _ => StatusCode::SERVICE_UNAVAILABLE.into_response(),
    }
}

/// Observes shared prefixes only while engine health and admission permit reads.
async fn shared_kv_prefix(
    State(state): State<AppState>,
    Json(request): Json<foretoken_model_protocol::KvPrefixRequest>,
) -> Response {
    if !state.health.ready() || !state.health.accepting() {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let Some(lookup) = state.shared_kv else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    match tokio::time::timeout(
        foretoken_model_protocol::KV_OBSERVATION_TIMEOUT,
        lookup.lookup(&request),
    )
    .await
    {
        Ok(Some(response)) => Json(response).into_response(),
        _ => StatusCode::SERVICE_UNAVAILABLE.into_response(),
    }
}

async fn healthz(State(state): State<AppState>) -> StatusCode {
    status(state.health.healthy())
}
async fn readyz(State(state): State<AppState>) -> StatusCode {
    status(state.health.ready())
}

/// Publishes backend and optional filesystem metrics as one OpenMetrics response.
async fn metrics(State(state): State<AppState>) -> Response {
    let encoded = (|| {
        let mut body = String::new();
        state.backend.encode_metrics(&mut body)?;
        if let Some(cache) = state.runtime_cache {
            let mut registry = Registry::default();
            registry.register_collector(Box::new(cache));
            encode_registry(&mut body, &registry)?;
        }
        encode_eof(&mut body)?;
        Ok::<_, std::fmt::Error>(body)
    })();
    match encoded {
        Ok(body) => (
            [(
                header::CONTENT_TYPE,
                HeaderValue::from_static(OPENMETRICS_CONTENT_TYPE),
            )],
            body,
        )
            .into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn metadata(State(state): State<AppState>) -> Json<RuntimeMetadataResponse> {
    Json(state.metadata)
}

async fn telemetry(State(state): State<AppState>) -> Json<TelemetryResponse> {
    Json(telemetry_response(&state))
}

/// Observes scheduler load on demand; stalled or unsupported utilities leave inference available.
async fn scheduler_load(State(state): State<AppState>) -> Response {
    match tokio::time::timeout(
        std::time::Duration::from_secs(5),
        state.backend.scheduler_load(),
    )
    .await
    {
        Ok(Ok(engines)) => Json(SchedulerLoadResponse {
            version: 1,
            collected_at_unix_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock predates Unix epoch")
                .as_millis() as u64,
            engines,
        })
        .into_response(),
        Ok(Err(_)) | Err(_) => StatusCode::SERVICE_UNAVAILABLE.into_response(),
    }
}

/// Closes admission before drain and returns post-close telemetry to the coordinator.
/// The coordinator retains server and native-process shutdown ownership.
async fn close_admission(State(state): State<AppState>) -> Json<TelemetryResponse> {
    if let Some(profiling) = &state.profiling {
        profiling.close();
    }
    state.health.set_accepting(false);
    Json(telemetry_response(&state))
}

/// Combines backend measurements and instance occupancy for telemetry and drain callers.
fn telemetry_response(state: &AppState) -> TelemetryResponse {
    let telemetry = state.backend.telemetry();
    TelemetryResponse {
        version: 3,
        request_cost: telemetry.request_cost,
        collected_at_unix_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .try_into()
            .unwrap_or(u64::MAX),
        accepting: state.health.accepting(),
        data_parallel_ranks: telemetry.data_parallel_ranks,
        running_requests: state.health.running_requests(),
        max_concurrent_requests: telemetry.max_concurrent_requests,
        scheduler_running_requests: telemetry.scheduler_running_requests,
        scheduler_waiting_requests: telemetry.scheduler_waiting_requests,
        kv_cache_usage: telemetry.kv_cache_usage,
        prompt_tokens_total: telemetry.prompt_tokens_total,
        generation_tokens_total: telemetry.generation_tokens_total,
        ttft_seconds: telemetry.ttft_seconds,
        tpot_seconds: telemetry.tpot_seconds,
        e2e_seconds: telemetry.e2e_seconds,
    }
}

/// Decodes explicit frontend cancellation and asks the backend to abort its request IDs.
/// The execution task, not this acknowledgement, retains termination ownership.
async fn abort(
    State(state): State<AppState>,
    input: Result<Json<AbortInput>, JsonRejection>,
) -> Result<StatusCode, ApiError> {
    let Json(input) = input.map_err(|_| ApiError::InvalidRequest)?;
    if !state.health.healthy() {
        return Err(ApiError::Unavailable);
    }
    if input.request_ids.is_empty() {
        return Err(ApiError::InvalidRequest);
    }
    state
        .backend
        .abort(&input.request_ids)
        .await
        .map_err(ApiError::backend)?;
    Ok(StatusCode::NO_CONTENT)
}

/// Translates a frontend cursor into a bounded rank-local delta or its reset signal.
/// The adapter retains cursor history; the handler only publishes the wire response.
async fn kv_index_delta(
    State(state): State<AppState>,
    Query(query): Query<KvDeltaQuery>,
) -> Response {
    let Some(adapter) = state.kv_events else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    match adapter.delta(
        query.dp_rank,
        query.epoch.as_deref(),
        query.after,
        query.limit.unwrap_or(256),
    ) {
        Ok(delta) => (StatusCode::OK, Json(delta)).into_response(),
        Err(KvDeltaError::Unavailable) => StatusCode::SERVICE_UNAVAILABLE.into_response(),
        Err(KvDeltaError::CursorReset(clear)) => {
            (StatusCode::CONFLICT, Json(clear)).into_response()
        }
    }
}
fn content_type_is(headers: &HeaderMap, expected: &str) -> bool {
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|value| value.trim().eq_ignore_ascii_case(expected))
}

fn status(healthy: bool) -> StatusCode {
    if healthy {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

impl ApiError {
    fn backend(error: BackendError) -> Self {
        match error {
            BackendError::InvalidRequest => Self::InvalidRequest,
            BackendError::Unavailable => Self::Unavailable,
            BackendError::Rejected => Self::Rejected,
            BackendError::Protocol => Self::Protocol,
            BackendError::RequestFailed => Self::RequestFailed,
        }
    }
    const fn status(self) -> StatusCode {
        match self {
            Self::InvalidRequest => StatusCode::BAD_REQUEST,
            Self::Unavailable | Self::Busy | Self::AdmissionUnavailable => {
                StatusCode::SERVICE_UNAVAILABLE
            }
            Self::Rejected | Self::Protocol | Self::RequestFailed => StatusCode::BAD_GATEWAY,
        }
    }
    const fn code(self) -> &'static str {
        match self {
            Self::InvalidRequest => "invalid_request",
            Self::Unavailable => "unavailable",
            Self::Busy => "admission_busy",
            Self::AdmissionUnavailable => "admission_unavailable",
            Self::Rejected => "rejected",
            Self::Protocol => "protocol",
            Self::RequestFailed => "request_failed",
        }
    }
    const fn message(self) -> &'static str {
        match self {
            Self::InvalidRequest => "invalid internal request",
            Self::Unavailable => "model server is unavailable",
            Self::Busy => "model server acceptance capacity is full",
            Self::AdmissionUnavailable => "model server admission membership is not published",
            Self::Rejected => "model server rejected the request",
            Self::Protocol => "model server protocol failed",
            Self::RequestFailed => "model server request failed",
        }
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status(),
            Json(ErrorBody {
                error: ErrorDetail {
                    code: self.code(),
                    message: self.message(),
                },
            }),
        )
            .into_response()
    }
}

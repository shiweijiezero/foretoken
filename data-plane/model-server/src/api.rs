// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Restricted internal HTTP routes for already-tokenized EngineCore requests.

use std::convert::Infallible;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use axum::body::Body;
use axum::extract::rejection::JsonRejection;
use axum::extract::{Query, State};
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use bytes::Bytes;
use futures::StreamExt;
use serde::Serialize;

use crate::backend::{Backend, BackendError, GenerateInput, TokenEvent};
use crate::kv_event_adapter::{KvDeltaError, KvEventAdapter};
use foretoken_model_protocol::{
    AbortInput, KvDeltaQuery, RuntimeMetadataResponse, TelemetryResponse,
};

// One atomic word linearizes admission close against request acceptance.
const ADMISSION_OPEN: u64 = 1 << 63;
const RUNNING_REQUESTS_MASK: u64 = !ADMISSION_OPEN;

/// Engine state used for API health and admission only; process ownership stays upstream.
#[derive(Default)]
pub struct RuntimeHealth {
    process_alive: AtomicBool,
    client_healthy: AtomicBool,
    admission: AtomicU64,
}

impl RuntimeHealth {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn set_process_alive(&self, value: bool) {
        self.process_alive.store(value, Ordering::Release);
    }
    pub fn set_client_healthy(&self, value: bool) {
        self.client_healthy.store(value, Ordering::Release);
    }
    pub fn set_accepting(&self, value: bool) {
        if value {
            self.admission.fetch_or(ADMISSION_OPEN, Ordering::AcqRel);
        } else {
            self.admission
                .fetch_and(RUNNING_REQUESTS_MASK, Ordering::AcqRel);
        }
    }
    pub fn healthy(&self) -> bool {
        self.process_alive.load(Ordering::Acquire) && self.client_healthy.load(Ordering::Acquire)
    }
    pub fn ready(&self) -> bool {
        self.healthy() && self.accepting()
    }
    pub fn accepting(&self) -> bool {
        self.admission.load(Ordering::Acquire) & ADMISSION_OPEN != 0
    }
    fn try_admit(self: &Arc<Self>) -> Option<AdmissionPermit> {
        let mut current = self.admission.load(Ordering::Acquire);
        loop {
            if current & ADMISSION_OPEN == 0 {
                return None;
            }
            let next = current + 1;
            match self.admission.compare_exchange_weak(
                current,
                next,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => {
                    return Some(AdmissionPermit {
                        health: self.clone(),
                    });
                }
                Err(observed) => current = observed,
            }
        }
    }
    fn running_requests(&self) -> u64 {
        self.admission.load(Ordering::Acquire) & RUNNING_REQUESTS_MASK
    }
}

struct AdmissionPermit {
    health: Arc<RuntimeHealth>,
}

impl Drop for AdmissionPermit {
    fn drop(&mut self) {
        self.health.admission.fetch_sub(1, Ordering::AcqRel);
    }
}

/// Mutable process state shared by typed HTTP handlers.
#[derive(Clone)]
pub struct AppState {
    backend: Arc<dyn Backend>,
    health: Arc<RuntimeHealth>,
    metadata: RuntimeMetadataResponse,
    kv_events: Option<Arc<KvEventAdapter>>,
}
impl AppState {
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
        }
    }
    pub fn with_kv_events(mut self, adapter: Arc<KvEventAdapter>) -> Self {
        self.kv_events = Some(adapter);
        self
    }
}

/// Construct only the group-local API; this is not an OpenAI-compatible router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/v1/internal/metadata", get(metadata))
        .route("/v1/internal/telemetry", get(telemetry))
        .route("/v1/internal/admission/close", post(close_admission))
        .route("/v1/internal/generate", post(generate))
        .route("/v1/internal/abort", post(abort))
        .route("/v1/internal/kv-index/delta", get(kv_index_delta))
        .with_state(state)
}
async fn healthz(State(state): State<AppState>) -> StatusCode {
    status(state.health.healthy())
}
async fn readyz(State(state): State<AppState>) -> StatusCode {
    status(state.health.ready())
}

async fn metadata(State(state): State<AppState>) -> Json<RuntimeMetadataResponse> {
    Json(state.metadata)
}

async fn telemetry(State(state): State<AppState>) -> Json<TelemetryResponse> {
    Json(telemetry_response(&state))
}

async fn close_admission(State(state): State<AppState>) -> Json<TelemetryResponse> {
    state.health.set_accepting(false);
    Json(telemetry_response(&state))
}

fn telemetry_response(state: &AppState) -> TelemetryResponse {
    let telemetry = state.backend.telemetry();
    TelemetryResponse {
        version: 1,
        accepting: state.health.accepting(),
        running_requests: state
            .health
            .running_requests()
            .max(telemetry.running_requests),
        max_concurrent_requests: telemetry.max_concurrent_requests,
    }
}

async fn generate(
    State(state): State<AppState>,
    input: Result<Json<GenerateInput>, JsonRejection>,
) -> Result<Response, ApiError> {
    let Json(input) = input.map_err(|_| ApiError::InvalidRequest)?;
    if !state.health.healthy() {
        return Err(ApiError::Unavailable);
    }
    let permit = state.health.try_admit().ok_or(ApiError::Unavailable)?;
    let request_id = input.request_id.clone();
    let stream = state
        .backend
        .generate(input)
        .await
        .map_err(ApiError::backend)?;
    let body_stream = stream.map(move |item| {
        let _permit = &permit;
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

async fn kv_index_delta(
    State(state): State<AppState>,
    Query(query): Query<KvDeltaQuery>,
) -> Response {
    let Some(adapter) = state.kv_events else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    let epoch = query.epoch.unwrap_or_default();
    match adapter.delta(&epoch, query.after.unwrap_or(0), query.limit.unwrap_or(256)) {
        Ok(delta) => (StatusCode::OK, Json(delta)).into_response(),
        Err(KvDeltaError::Unavailable) => StatusCode::SERVICE_UNAVAILABLE.into_response(),
        Err(KvDeltaError::CursorReset(clear)) => {
            (StatusCode::CONFLICT, Json(clear)).into_response()
        }
    }
}
fn status(healthy: bool) -> StatusCode {
    if healthy {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

/// Fixed, wire-safe errors for the internal API.
#[derive(Debug, Clone, Copy)]
enum ApiError {
    InvalidRequest,
    Unavailable,
    Rejected,
    Protocol,
    RequestFailed,
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
            Self::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
            Self::Rejected | Self::Protocol | Self::RequestFailed => StatusCode::BAD_GATEWAY,
        }
    }
    const fn code(self) -> &'static str {
        match self {
            Self::InvalidRequest => "invalid_request",
            Self::Unavailable => "unavailable",
            Self::Rejected => "rejected",
            Self::Protocol => "protocol",
            Self::RequestFailed => "request_failed",
        }
    }
    const fn message(self) -> &'static str {
        match self {
            Self::InvalidRequest => "invalid internal request",
            Self::Unavailable => "model server is unavailable",
            Self::Rejected => "model server rejected the request",
            Self::Protocol => "model server protocol failed",
            Self::RequestFailed => "model server request failed",
        }
    }
}
#[derive(Serialize)]
struct ErrorBody {
    error: ErrorDetail,
}
#[derive(Serialize)]
struct ErrorDetail {
    code: &'static str,
    message: &'static str,
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

#[cfg(test)]
#[path = "tests/api.rs"]
mod tests;

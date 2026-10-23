// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Proxies opaque video bodies and native telemetry while retaining each HTTP admission permit.

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, Method, Request, Response, StatusCode};
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use foretoken_model_protocol::{
    CumulativeHistogram, RuntimeMetadataResponse, RuntimeModelIdentity, TelemetryResponse,
};
use futures::StreamExt;
use hyproxy::headers::hopby::StripHopByHopLayer;

use crate::execution::{AdmissionPermit, RuntimeHealth};

#[derive(Clone)]
pub(super) struct AppState {
    pub(super) client: reqwest::Client,
    pub(super) upstream: String,
    pub(super) model: String,
    pub(super) revision: String,
    pub(super) health: Arc<RuntimeHealth>,
}

/// Builds video and diagnostic routes that borrow the serving supervisor's admission state.
pub(super) fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(healthz))
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/metrics", get(metrics).layer(StripHopByHopLayer::new()))
        .route("/v1/internal/metadata", get(metadata))
        .route("/v1/internal/telemetry", get(telemetry))
        .route("/v1/internal/admission/close", post(close_admission))
        .route(
            "/v1/videos/sync",
            post(proxy_video).layer(StripHopByHopLayer::new()),
        )
        .with_state(state)
}

async fn healthz(State(state): State<AppState>) -> StatusCode {
    status(runtime_healthy(&state).await)
}

async fn readyz(State(state): State<AppState>) -> StatusCode {
    status(runtime_healthy(&state).await)
}

/// Combines local process state with the native HTTP probe without admitting generation work.
async fn runtime_healthy(state: &AppState) -> bool {
    state.health.ready()
        && matches!(
            state
                .client
                .get(format!("{}/health", state.upstream))
                .timeout(Duration::from_secs(1))
                .send()
                .await,
            Ok(response) if response.status().is_success()
        )
}

async fn metadata(State(state): State<AppState>) -> Json<RuntimeMetadataResponse> {
    Json(RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: state.model,
            revision: state.revision,
        },
        model_dtype: None,
        effective_max_model_len: 0,
        max_logprobs: None,
        ec_transfer: None,
        prepared_tokenizer: None,
        capabilities: ["video".to_owned()].into_iter().collect(),
    })
}

async fn telemetry(State(state): State<AppState>) -> Json<TelemetryResponse> {
    Json(telemetry_response(&state.health))
}

/// Closes new intake while the serving supervisor retains accepted requests and process shutdown.
async fn close_admission(State(state): State<AppState>) -> Json<TelemetryResponse> {
    state.health.set_accepting(false);
    Json(telemetry_response(&state.health))
}

fn telemetry_response(health: &RuntimeHealth) -> TelemetryResponse {
    TelemetryResponse {
        version: 3,
        request_cost: Default::default(),
        collected_at_unix_ms: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .min(u128::from(u64::MAX)) as u64,
        accepting: health.accepting(),
        data_parallel_ranks: Vec::new(),
        running_requests: health.running_requests(),
        max_concurrent_requests: None,
        scheduler_running_requests: None,
        scheduler_waiting_requests: None,
        kv_cache_usage: None,
        prompt_tokens_total: None,
        generation_tokens_total: None,
        ttft_seconds: CumulativeHistogram::default(),
        tpot_seconds: CumulativeHistogram::default(),
        e2e_seconds: CumulativeHistogram::default(),
    }
}

/// Streams native metrics independently of video admission or engine preprocessing.
async fn metrics(State(state): State<AppState>) -> Response<Body> {
    proxy_request(
        &state,
        Method::GET,
        "/metrics",
        HeaderMap::new(),
        Body::empty(),
        None,
    )
    .await
}

/// Transfers the accepted video's permit to its upstream response stream, not a text executor.
async fn proxy_video(State(state): State<AppState>, request: Request<Body>) -> Response<Body> {
    if !state.health.ready() {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let Ok(guard) = state.health.try_admit() else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    let (parts, body) = request.into_parts();
    proxy_request(
        &state,
        Method::POST,
        "/v1/videos/sync",
        parts.headers,
        body,
        Some(guard),
    )
    .await
}

/// Proxies one exchange, retaining its admission permit until the HTTP response stream ends.
async fn proxy_request(
    state: &AppState,
    method: Method,
    path: &str,
    mut headers: HeaderMap,
    body: Body,
    guard: Option<AdmissionPermit>,
) -> Response<Body> {
    let has_body = method != Method::GET && method != Method::HEAD;
    headers.remove(axum::http::header::HOST);
    // This transport forwards data frames only, so neither direction advertises trailers.
    headers.remove(axum::http::header::TRAILER);
    let mut request = state
        .client
        .request(method, format!("{}{path}", state.upstream))
        .headers(headers);
    if has_body {
        request = request.body(reqwest::Body::wrap_stream(body.into_data_stream()));
    }
    let upstream = match request.send().await {
        Ok(response) => response,
        Err(_) => return StatusCode::BAD_GATEWAY.into_response(),
    };
    let status = upstream.status();
    let mut headers = upstream.headers().clone();
    headers.remove(axum::http::header::TRAILER);
    let mut bytes = Box::pin(upstream.bytes_stream());
    let stream = async_stream::stream! {
        let _guard = guard;
        while let Some(item) = bytes.next().await {
            yield item;
        }
    };
    let mut response = Response::new(Body::from_stream(stream));
    *response.status_mut() = status;
    *response.headers_mut() = headers;
    response
}

fn status(value: bool) -> StatusCode {
    if value {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

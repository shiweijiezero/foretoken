// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Composes client APIs, operator endpoints, and frontend-wide HTTP middleware.

use std::sync::Arc;
use std::time::Duration;

use axum::extract::{DefaultBodyLimit, State};
use axum::http::StatusCode;
use axum::middleware;
use axum::response::Response;
use axum::routing::get;
use axum::{Json, Router};

use crate::api::{self, ApiState};
use crate::runtime::Generation;

const MAX_HTTP_BODY_BYTES: usize = 48 * 1024 * 1024;

/// Creates the frontend HTTP router with shared generation services, body limits, and metrics.
pub fn router(
    generation: Arc<dyn Generation>,
    models: Arc<dyn Fn() -> Vec<String> + Send + Sync>,
    stream_idle: Duration,
) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/statusz", get(statusz))
        .route("/metrics", get(metrics))
        .route(
            "/internal/autoscaling/telemetry",
            get(autoscaling_telemetry),
        )
        .merge(api::router())
        .with_state(ApiState {
            generation,
            models,
            stream_idle,
        })
        .layer(DefaultBodyLimit::max(MAX_HTTP_BODY_BYTES))
        .layer(middleware::from_fn(foretoken_metrics::track_http_metrics))
}

async fn healthz() -> StatusCode {
    StatusCode::OK
}

async fn readyz(State(state): State<ApiState>) -> StatusCode {
    if state.generation.ready() {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

async fn statusz(State(state): State<ApiState>) -> Json<crate::runtime::RuntimeDiagnostics> {
    Json(state.generation.diagnostics())
}

async fn autoscaling_telemetry() -> Json<foretoken_metrics::AutoscalingTelemetry> {
    Json(foretoken_metrics::autoscaling_telemetry())
}

async fn metrics(State(state): State<ApiState>) -> Response {
    let diagnostics = state.generation.diagnostics();
    foretoken_metrics::scrape_with_kv_index(
        &diagnostics.kv_index.state,
        diagnostics.kv_index.reason.as_deref(),
        diagnostics.kv_index.sources_healthy,
        diagnostics.kv_index.sources_total,
    )
    .await
}

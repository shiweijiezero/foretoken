// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Kubernetes-backed asynchronous video task endpoints.

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::json;

use super::ApiState;
use crate::video_task::VideoTaskRequest;

/// Public submission envelope; execution settings are supplied by the task service.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct VideoTaskSubmit {
    model_service_ref: ModelServiceReference,
    request: VideoTaskRequest,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelServiceReference {
    name: String,
}

/// HTTP acknowledgement with URLs for subsequent task operations.
#[derive(Serialize)]
struct VideoTaskAccepted {
    id: String,
    status_url: String,
    content_url: String,
}

/// Registers durable video submission, observation, cancellation, and result access.
pub(super) fn router() -> Router<ApiState> {
    Router::new()
        .route("/v1/videos", axum::routing::post(create))
        .route("/v1/videos/{id}", axum::routing::get(status).delete(delete))
        .route("/v1/videos/{id}/cancel", axum::routing::post(cancel))
        .route("/v1/videos/{id}/content", axum::routing::get(content))
}

async fn create(State(state): State<ApiState>, Json(request): Json<VideoTaskSubmit>) -> Response {
    let Some(client) = state.video_tasks else {
        return StatusCode::NOT_IMPLEMENTED.into_response();
    };
    match client
        .create(&request.model_service_ref.name, request.request)
        .await
    {
        Ok(id) => (
            StatusCode::ACCEPTED,
            Json(VideoTaskAccepted {
                status_url: format!("/v1/videos/{id}"),
                content_url: format!("/v1/videos/{id}/content"),
                id,
            }),
        )
            .into_response(),
        Err(status) => status.into_response(),
    }
}

async fn status(State(state): State<ApiState>, Path(id): Path<String>) -> Response {
    let Some(client) = state.video_tasks else {
        return StatusCode::NOT_IMPLEMENTED.into_response();
    };
    match client.get(&id).await {
        Ok(task) => Json(json!({
            "id": task.metadata.name,
            "phase": if task.status.phase.is_empty() { "Pending" } else { &task.status.phase },
            "reason": task.status.reason,
            "message": task.status.message,
            "status_url": format!("/v1/videos/{id}"),
            "content_url": format!("/v1/videos/{id}/content"),
        }))
        .into_response(),
        Err(status) => status.into_response(),
    }
}

async fn cancel(State(state): State<ApiState>, Path(id): Path<String>) -> Response {
    let Some(client) = state.video_tasks else {
        return StatusCode::NOT_IMPLEMENTED.into_response();
    };
    match client.cancel(&id).await {
        Ok(()) => StatusCode::ACCEPTED.into_response(),
        Err(status) => status.into_response(),
    }
}

async fn delete(State(state): State<ApiState>, Path(id): Path<String>) -> Response {
    let Some(client) = state.video_tasks else {
        return StatusCode::NOT_IMPLEMENTED.into_response();
    };
    match client.delete(&id).await {
        Ok(()) => StatusCode::ACCEPTED.into_response(),
        Err(status) => status.into_response(),
    }
}

async fn content(State(state): State<ApiState>, Path(id): Path<String>) -> Response {
    let Some(client) = state.video_tasks else {
        return StatusCode::NOT_IMPLEMENTED.into_response();
    };
    match client.content(&id).await {
        Ok(bytes) => ([(axum::http::header::CONTENT_TYPE, "video/mp4")], bytes).into_response(),
        Err(status) => status.into_response(),
    }
}

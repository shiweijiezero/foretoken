// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Forwards opaque video inputs after the shared Router has selected a backend.

use std::time::Duration;

use axum::body::Body;
use axum::http::HeaderMap;
use axum::response::Response;
use foretoken_router::RouteSession;
use futures::StreamExt;

use crate::GenerationError;

/// Private worker-to-frontend binding, consumed before forwarding native video requests.
pub(crate) const MODEL_SERVICE_UID_HEADER: &str = "x-foretoken-model-service-uid";

/// Multipart video request; the engine owns media parsing and generation parameters.
pub struct VideoRequest {
    pub model: String,
    pub request_id: String,
    /// Durable workers bind execution to the catalog service that admitted the task.
    pub model_service_uid: Option<String>,
    pub headers: HeaderMap,
    /// Anonymous request file; dispatch owns its handle until upload completes or is cancelled.
    pub body: tokio::fs::File,
}

/// Forwards a Router-selected video request and transfers route ownership to the returned body.
/// Transport failure or consumer drop releases the session without replaying ambiguous GPU work.
pub(crate) async fn forward(
    client: &reqwest::Client,
    endpoint: &str,
    request: VideoRequest,
    mut session: Box<dyn RouteSession>,
    timeout: Duration,
) -> Result<Response, GenerationError> {
    let mut headers = request.headers;
    headers.remove(axum::http::header::HOST);
    // This transport forwards data frames only, so neither direction advertises trailers.
    headers.remove(axum::http::header::TRAILER);
    // The route's hop-header layer sanitizes both directions; generation is never retried.
    let response = client
        .post(format!("{}/v1/videos/sync", endpoint.trim_end_matches('/')))
        .headers(headers)
        .timeout(timeout)
        .body(request.body)
        .send()
        .await
        .map_err(|_| GenerationError::RequestFailed)?;
    session.response_started();
    let status = response.status();
    let mut headers = response.headers().clone();
    headers.remove(axum::http::header::TRAILER);
    let stream = async_stream::stream! {
        let mut bytes = response.bytes_stream();
        while let Some(chunk) = bytes.next().await {
            let failed = chunk.is_err();
            yield chunk;
            if failed { break; }
        }
        session.stage_complete();
    };
    // The stream owns the route reservation until completion or client disconnect.
    let mut response = Response::new(Body::from_stream(stream));
    *response.status_mut() = status;
    *response.headers_mut() = headers;
    Ok(response)
}

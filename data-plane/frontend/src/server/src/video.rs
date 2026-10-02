// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Forwards opaque video inputs after the shared Router has selected a backend.

use std::time::Duration;

use axum::body::Body;
use axum::http::{HeaderMap, HeaderName};
use axum::response::Response;
use foretoken_router::RouteSession;
use futures::StreamExt;

use crate::GenerationError;

/// Multipart video request; the engine owns media parsing and generation parameters.
pub struct VideoRequest {
    pub model: String,
    pub request_id: String,
    pub headers: HeaderMap,
    /// Anonymous request file; dispatch owns its handle until upload completes or is cancelled.
    pub body: tokio::fs::File,
}

pub(crate) async fn forward(
    client: &reqwest::Client,
    endpoint: &str,
    request: VideoRequest,
    mut session: Box<dyn RouteSession>,
    timeout: Duration,
) -> Result<Response, GenerationError> {
    let mut upstream = client.post(format!("{}/v1/videos/sync", endpoint.trim_end_matches('/')));
    for (name, value) in &request.headers {
        if !hop_by_hop(name) && name != axum::http::header::HOST {
            upstream = upstream.header(name, value);
        }
    }
    // Do not retry generation: an ambiguous transport failure may already have started GPU work.
    let response = upstream
        .timeout(timeout)
        .body(request.body)
        .send()
        .await
        .map_err(|_| GenerationError::RequestFailed)?;
    session.response_started();
    let mut builder = Response::builder().status(response.status());
    for (name, value) in response.headers() {
        if !hop_by_hop(name) {
            builder = builder.header(name, value);
        }
    }
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
    builder
        .body(Body::from_stream(stream))
        .map_err(|_| GenerationError::Internal)
}

fn hop_by_hop(name: &HeaderName) -> bool {
    matches!(
        name.as_str(),
        "connection"
            | "keep-alive"
            | "proxy-authenticate"
            | "proxy-authorization"
            | "te"
            | "trailer"
            | "transfer-encoding"
            | "upgrade"
    )
}

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Resolves multipart video model identity without changing engine-owned media inputs.

use axum::extract::{FromRequest, Multipart, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};

use super::{ApiState, openai_error, resolve_model, server_request_id};
use crate::http::MAX_HTTP_BODY_BYTES;
use crate::runtime::GenerationError;

/// Inspects the routing field and hands the unchanged multipart body to the shared runtime.
pub(super) async fn generate(State(state): State<ApiState>, request: Request) -> Response {
    let (parts, incoming) = request.into_parts();
    let headers = parts.headers.clone();
    let body = match axum::body::to_bytes(incoming, MAX_HTTP_BODY_BYTES).await {
        Ok(body) => body,
        Err(_) => return StatusCode::PAYLOAD_TOO_LARGE.into_response(),
    };
    let result = async {
        // Keep the original body-limit extension; media parsing belongs to the engine.
        let parse_request =
            axum::http::Request::from_parts(parts, axum::body::Body::from(body.clone()));
        let mut multipart = Multipart::from_request(parse_request, &())
            .await
            .map_err(|_| GenerationError::InvalidRequest)?;
        let mut requested = None;
        while let Some(field) = multipart
            .next_field()
            .await
            .map_err(|_| GenerationError::InvalidRequest)?
        {
            if field.name() == Some("model") {
                if requested.is_some() || field.file_name().is_some() {
                    return Err(GenerationError::InvalidRequest);
                }
                let value = field
                    .text()
                    .await
                    .map_err(|_| GenerationError::InvalidRequest)?;
                if value.is_empty() || value.len() > 1024 {
                    return Err(GenerationError::InvalidRequest);
                }
                requested = Some(value);
            }
        }
        let model = resolve_model(&state, requested)?;
        state
            .generation
            .generate_video(crate::VideoRequest {
                model,
                request_id: server_request_id("video"),
                headers,
                body,
            })
            .await
    }
    .await;
    result.unwrap_or_else(openai_error)
}

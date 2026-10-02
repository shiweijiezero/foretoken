// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Resolves multipart video identity while keeping media bodies out of frontend memory.

use axum::extract::{DefaultBodyLimit, FromRequest, Multipart, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use futures::StreamExt;
use tokio::io::{AsyncSeekExt, AsyncWriteExt};
use tokio_util::io::ReaderStream;

use super::{ApiState, openai_error, resolve_model, server_request_id};
use crate::runtime::GenerationError;

/// Stages a request until its model is known, then transfers the file to backend dispatch.
pub(super) async fn generate(State(state): State<ApiState>, request: Request) -> Response {
    let (parts, incoming) = request.into_parts();
    let headers = parts.headers.clone();
    // An anonymous file preserves the multipart bytes regardless of field order. Its handles
    // own cleanup on parse failure, cancellation, and completion, without a persistent file name.
    let file = match tokio::task::spawn_blocking(tempfile::tempfile).await {
        Ok(Ok(file)) => file,
        _ => return openai_error(GenerationError::Internal),
    };
    let mut file = tokio::fs::File::from_std(file);
    let mut stream = incoming.into_data_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = match chunk {
            Ok(chunk) => chunk,
            Err(_) => return openai_error(GenerationError::InvalidRequest),
        };
        if file.write_all(&chunk).await.is_err() {
            return StatusCode::INSUFFICIENT_STORAGE.into_response();
        }
    }
    let result = async {
        file.flush().await.map_err(|_| GenerationError::Internal)?;
        file.rewind().await.map_err(|_| GenerationError::Internal)?;
        let parse_file = file
            .try_clone()
            .await
            .map_err(|_| GenerationError::Internal)?;
        // Video media is disk-backed; the JSON endpoint body limit does not apply here.
        let mut parse_request = axum::http::Request::from_parts(
            parts,
            axum::body::Body::from_stream(ReaderStream::new(parse_file)),
        );
        DefaultBodyLimit::disable().apply(&mut parse_request);
        let mut multipart = Multipart::from_request(parse_request, &())
            .await
            .map_err(|_| GenerationError::InvalidRequest)?;
        let mut requested = None;
        while let Some(mut field) = multipart
            .next_field()
            .await
            .map_err(|_| GenerationError::InvalidRequest)?
        {
            if field.name() == Some("model") {
                if requested.is_some() || field.file_name().is_some() {
                    return Err(GenerationError::InvalidRequest);
                }
                // Model identifiers retain the existing API bound; media fields have no size cap.
                let mut value = Vec::new();
                while let Some(chunk) = field
                    .chunk()
                    .await
                    .map_err(|_| GenerationError::InvalidRequest)?
                {
                    if value.len() + chunk.len() > 1024 {
                        return Err(GenerationError::InvalidRequest);
                    }
                    value.extend_from_slice(&chunk);
                }
                if value.is_empty() {
                    return Err(GenerationError::InvalidRequest);
                }
                requested =
                    Some(String::from_utf8(value).map_err(|_| GenerationError::InvalidRequest)?);
            } else {
                while field
                    .chunk()
                    .await
                    .map_err(|_| GenerationError::InvalidRequest)?
                    .is_some()
                {}
            }
        }
        drop(multipart);
        let model = resolve_model(&state, requested)?;
        file.rewind().await.map_err(|_| GenerationError::Internal)?;
        state
            .generation
            .generate_video(crate::VideoRequest {
                model,
                request_id: server_request_id("video"),
                headers,
                body: file,
            })
            .await
    }
    .await;
    result.unwrap_or_else(openai_error)
}

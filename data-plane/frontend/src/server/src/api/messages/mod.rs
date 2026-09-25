// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Anthropic Messages adapters over the shared vLLM Rust chat pipeline.
//!
//! Request types/lowering and error envelopes derive from vLLM PR #52896
//! (1e19c0826853371a5549f23d83678b7e56b8baea). Transport execution belongs to
//! Foretoken's Generation interface; this module does not own model processes.

mod convert;
mod error;
mod output;
mod types;

use axum::extract::State;
use axum::extract::rejection::JsonRejection;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use serde_json::json;

use self::convert::{prepare_count_tokens_request, prepare_messages_request};
use self::error::AnthropicApiError;
use self::types::{AnthropicCountTokensRequest, AnthropicMessagesRequest};
use super::{ApiState, RequestTiming, server_request_id};

/// Registers Messages generation and exact prompt token counting.
pub(super) fn router() -> Router<ApiState> {
    Router::new()
        .route("/v1/messages", post(messages))
        .route("/v1/messages/count_tokens", post(count_tokens))
}

/// Lowers an Anthropic request and dispatches through the shared generation service.
async fn messages(
    State(state): State<ApiState>,
    request: Result<Json<AnthropicMessagesRequest>, JsonRejection>,
) -> Response {
    let timing = RequestTiming::now();
    let Json(request) = match request {
        Ok(request) => request,
        Err(error) => return json_error(error).into_response(),
    };
    let (model, chat, include_reasoning) =
        match prepare_messages_request(request, server_request_id("msg")) {
            Ok(request) => request,
            Err(error) => return error.into_response(),
        };
    let stream = chat.intermediate;
    match state
        .generate_chat(model, chat, include_reasoning, timing)
        .await
    {
        Ok(generated) if stream => output::streaming(generated, state.stream_idle),
        Ok(generated) => output::collected(generated, state.stream_idle).await,
        Err(error) => AnthropicApiError::from(error).into_response(),
    }
}

/// Counts the actual rendered prompt without submitting an inference request.
async fn count_tokens(
    State(state): State<ApiState>,
    request: Result<Json<AnthropicCountTokensRequest>, JsonRejection>,
) -> Response {
    let Json(request) = match request {
        Ok(request) => request,
        Err(error) => return json_error(error).into_response(),
    };
    let (model, chat) = match prepare_count_tokens_request(request, server_request_id("count")) {
        Ok(request) => request,
        Err(error) => return error.into_response(),
    };
    match state.generation.tokenize_chat(&model, chat, false).await {
        Ok(tokens) => Json(json!({"input_tokens": tokens.token_ids.len()})).into_response(),
        Err(error) => AnthropicApiError::from(error).into_response(),
    }
}

/// Keeps extraction failures inside the Anthropic envelope, including the parent's body limit.
fn json_error(error: JsonRejection) -> AnthropicApiError {
    if error.status() == StatusCode::PAYLOAD_TOO_LARGE {
        AnthropicApiError {
            status: StatusCode::PAYLOAD_TOO_LARGE,
            kind: "request_too_large",
            message: "Request body exceeds the service limit".into(),
        }
    } else {
        AnthropicApiError::invalid("Invalid Messages request body")
    }
}

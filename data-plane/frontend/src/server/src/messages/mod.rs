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

use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::extract::State;
use axum::extract::rejection::JsonRejection;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use foretoken_chat::ParserSelection;
use foretoken_text::Prompt;
use serde_json::json;

use self::convert::{prepare_count_tokens_request, prepare_messages_request};
use self::error::AnthropicApiError;
use self::types::{AnthropicCountTokensRequest, AnthropicMessagesRequest};
use crate::http::server_request_id;
use crate::runtime::{Generation, GenerationRequest};

#[derive(Clone)]
struct MessagesState {
    generation: Arc<dyn Generation>,
    stream_idle: Duration,
}

/// Adds Messages and exact token counting to the frontend's shared HTTP router.
///
/// The parent router owns body limits and metrics; each request owns its output stream.
pub(crate) fn router(generation: Arc<dyn Generation>, stream_idle: Duration) -> Router {
    Router::new()
        .route("/v1/messages", post(messages))
        .route("/v1/messages/count_tokens", post(count_tokens))
        .with_state(MessagesState {
            generation,
            stream_idle,
        })
}

/// Lowers an Anthropic request and dispatches through the canonical generation lifecycle.
async fn messages(
    State(state): State<MessagesState>,
    request: Result<Json<AnthropicMessagesRequest>, JsonRejection>,
) -> Response {
    let started_at = Instant::now();
    let arrival_time = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|time| time.as_secs_f64());
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
    let tool_call_parser = if chat.tool_context.parsing_enabled() {
        ParserSelection::Auto
    } else {
        ParserSelection::None
    };
    let generation = GenerationRequest {
        model,
        request_id: chat.request_id.clone(),
        prompt: Prompt::Text(String::new()),
        sampling_params: chat.sampling_params.clone(),
        decode_options: chat.decode_options.clone(),
        intermediate: stream,
        priority: chat.priority,
        cache_salt: chat.cache_salt.clone(),
        session_id: chat.session_id.clone(),
        arrival_time,
        started_at,
        tool_call_parser,
        reasoning_parser: ParserSelection::Auto,
    };
    match state
        .generation
        .generate_chat(generation, chat, include_reasoning)
        .await
    {
        Ok(generated) if stream => output::streaming(generated, state.stream_idle),
        Ok(generated) => output::collected(generated, state.stream_idle).await,
        Err(error) => AnthropicApiError::from(error).into_response(),
    }
}

/// Counts the actual rendered prompt without submitting an inference request.
async fn count_tokens(
    State(state): State<MessagesState>,
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

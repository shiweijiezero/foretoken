// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the vLLM project
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Stateless Responses serving adapted from vLLM PR #53380, commit
//! 9de2bc119009c3e37e36ddbe240c5647d848f752. Foretoken retains execution ownership.

mod convert;
mod error;
mod streaming;
mod tools;
mod types;

use std::collections::BTreeSet;
use std::convert::Infallible;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::extract::State;
use axum::extract::rejection::JsonRejection;
use axum::response::sse::{Event, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use foretoken_chat::{ChatEvent, FinishReason, ParserSelection};
use foretoken_text::Prompt;
use futures::{Stream, StreamExt};
use serde_json::json;
use uuid::Uuid;

use crate::runtime::{Generation, GenerationError, GenerationRequest};
use convert::{ResponseMeta, build_output_items, build_response, build_usage};
use error::ApiError;
use streaming::{
    OutputItemStreamer, ResponseStreamEvent, response_lifecycle_event, restore_tool_event,
};
use types::{ResponseItemStatus, ResponsesRequest};

#[derive(Clone)]
struct ResponsesState {
    generation: Arc<dyn Generation>,
    stream_idle: Duration,
}

/// Add Responses routes to the frontend while sharing its generation and idle-timeout owners.
pub(crate) fn router(generation: Arc<dyn Generation>, stream_idle: Duration) -> Router {
    Router::new()
        .route("/v1/responses", post(create))
        .with_state(ResponsesState {
            generation,
            stream_idle,
        })
}

/// Lower a Responses request once, then expose the shared chat output as JSON or SSE.
async fn create(
    State(state): State<ResponsesState>,
    body: Result<Json<ResponsesRequest>, JsonRejection>,
) -> Response {
    let started_at = Instant::now();
    let arrival_time = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|v| v.as_secs_f64());
    let body = match body {
        Ok(Json(body)) => body,
        Err(error) => {
            let mut response = ApiError::invalid_request("Invalid Responses request body", None);
            if error.status() == axum::http::StatusCode::PAYLOAD_TOO_LARGE {
                response.status = axum::http::StatusCode::PAYLOAD_TOO_LARGE;
                response.body["message"] = json!("Request body exceeds the service limit");
            }
            return response.into_response();
        }
    };
    let stream_requested = body.stream;
    // The frontend owns backend identities; every response event uses this same ID.
    let request_id = format!("resp_{}", Uuid::new_v4().simple());
    let (chat, meta) = match convert::prepare_responses_request(body, request_id.clone()) {
        Ok(prepared) => prepared,
        Err(error) => return error.into_response(),
    };
    let request = GenerationRequest {
        model: meta.model.clone(),
        request_id: request_id.clone(),
        prompt: Prompt::Text(String::new()),
        sampling_params: chat.sampling_params.clone(),
        decode_options: chat.decode_options.clone(),
        intermediate: stream_requested,
        priority: chat.priority,
        cache_salt: chat.cache_salt.clone(),
        session_id: chat.session_id.clone(),
        arrival_time,
        started_at,
        tool_call_parser: if chat.tool_context.parsing_enabled() {
            ParserSelection::Auto
        } else {
            ParserSelection::None
        },
        reasoning_parser: ParserSelection::Auto,
    };
    let generated = match state
        .generation
        .generate_chat(request, chat, meta.include_reasoning)
        .await
    {
        Ok(generated) => generated,
        Err(error) => return ApiError::generation(error).into_response(),
    };
    let (_, events) = match crate::response::chat_events(generated, state.stream_idle) {
        Ok(events) => events,
        Err(_) => return ApiError::generation(GenerationError::Internal).into_response(),
    };
    let created_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    if stream_requested {
        Sse::new(stream(events, meta, request_id, created_at)).into_response()
    } else {
        collected(events, meta, request_id, created_at).await
    }
}

/// Assemble collected output from the same parser events used for streaming responses.
async fn collected(
    events: impl Stream<Item = foretoken_chat::Result<ChatEvent>> + Send,
    meta: ResponseMeta,
    request_id: String,
    created_at: u64,
) -> Response {
    let mut events = Box::pin(events);
    while let Some(event) = events.next().await {
        match event {
            Ok(ChatEvent::Done {
                message,
                usage,
                finish_reason,
                ..
            }) => {
                if matches!(finish_reason, FinishReason::Error | FinishReason::Abort) {
                    return ApiError::generation(GenerationError::RequestFailed).into_response();
                }
                let response = build_response(
                    &meta,
                    &request_id,
                    created_at,
                    build_output_items(&message, meta.include_reasoning),
                    response_status(&finish_reason),
                    Some(build_usage(&usage)),
                );
                let mut response = serde_json::to_value(response).expect("response serialization");
                if restore_tools(&mut response, &meta.tool_names).is_err() {
                    return ApiError::generation(GenerationError::BackendProtocol).into_response();
                }
                return Json(response).into_response();
            }
            Ok(_) => {}
            Err(_) => return ApiError::generation(GenerationError::RequestFailed).into_response(),
        }
    }
    ApiError::generation(GenerationError::BackendProtocol).into_response()
}

/// Map terminal generation state to its Responses lifecycle state.
fn response_status(reason: &FinishReason) -> ResponseItemStatus {
    match reason {
        FinishReason::Length => ResponseItemStatus::Incomplete,
        FinishReason::Abort | FinishReason::Error => ResponseItemStatus::Failed,
        FinishReason::Stop(_) | FinishReason::Repetition(_) => ResponseItemStatus::Completed,
    }
}

/// Encode one protocol event, assigning a single monotonically increasing request-local sequence.
fn sse(event: ResponseStreamEvent, sequence: &mut u64) -> Result<Event, Infallible> {
    let result = Event::default()
        .event(event.event_type())
        .data(event.to_json(*sequence));
    *sequence += 1;
    Ok(result)
}

/// Adapt structured events directly to Responses SSE; dropping this stream cancels the backend.
fn stream(
    events: impl Stream<Item = foretoken_chat::Result<ChatEvent>> + Send + 'static,
    meta: ResponseMeta,
    request_id: String,
    created_at: u64,
) -> impl Stream<Item = Result<Event, Infallible>> + Send {
    async_stream::stream! {
        let mut sequence = 0;
        let mut events = Box::pin(events);
        let mut items = OutputItemStreamer::new(meta.include_reasoning);
        let initial = build_response(&meta, &request_id, created_at, vec![], ResponseItemStatus::InProgress, None);
        yield sse(response_lifecycle_event("response.created", &initial), &mut sequence);
        yield sse(response_lifecycle_event("response.in_progress", &initial), &mut sequence);
        let mut custom_items = BTreeSet::new();
        while let Some(event) = events.next().await {
            match event {
                Ok(ChatEvent::Done { usage, finish_reason, .. }) => {
                    let status = response_status(&finish_reason);
                    if status == ResponseItemStatus::Failed { break; }
                    let closing = items.on_stream_end().into_iter()
                        .map(|event| restore_tool_event(event, &meta.tool_names, &mut custom_items))
                        .collect::<Result<Vec<_>, _>>();
                    let Ok(closing) = closing else { break; };
                    let response = build_response(&meta, &request_id, created_at,
                        items.final_output_items(), status, Some(build_usage(&usage)));
                    let event_type = if status == ResponseItemStatus::Incomplete { "response.incomplete" } else { "response.completed" };
                    let final_events = restore_tool_event(response_lifecycle_event(event_type, &response), &meta.tool_names, &mut custom_items);
                    let Ok(final_events) = final_events else { break; };
                    // Release backend ownership before terminal output can block on a slow client.
                    drop(events);
                    for event in closing.into_iter().flatten() { yield sse(event, &mut sequence); }
                    for event in final_events { yield sse(event, &mut sequence); }
                    return;
                }
                Ok(event) => {
                    let converted = items.on_event(&event).into_iter()
                        .map(|event| restore_tool_event(event, &meta.tool_names, &mut custom_items))
                        .collect::<Result<Vec<_>, _>>();
                    let Ok(converted) = converted else { break; };
                    for event in converted.into_iter().flatten() { yield sse(event, &mut sequence); }
                }
                Err(_) => break,
            }
        }
        drop(events);
        let mut response = build_response(&meta, &request_id, created_at, vec![], ResponseItemStatus::Failed, None);
        response.error = Some(json!({"code":"generation_failed","message":"Response generation failed"}));
        yield sse(response_lifecycle_event("response.failed", &response), &mut sequence);
    }
}

/// Restore external tool names in one response envelope after shared output assembly.
fn restore_tools(
    response: &mut serde_json::Value,
    names: &tools::ToolNames,
) -> Result<(), ApiError> {
    if let Some(items) = response
        .get_mut("output")
        .and_then(serde_json::Value::as_array_mut)
    {
        for item in items {
            names.restore_item(item)?;
        }
    }
    Ok(())
}

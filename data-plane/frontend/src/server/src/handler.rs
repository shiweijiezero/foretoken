// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! HTTP handlers for the frontend routes.

use std::convert::Infallible;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};

use foretoken_chat::{ChatFacade, Message, Role};
use foretoken_model_protocol::{GenerateInput, RouteStage, TokenEvent};

use crate::dto::{
    ChatCompletion, ChatCompletionChunk, ChatCompletionRequest, ChatMessage, ChatRole, ChunkChoice,
    CompletionChoice, Delta,
};
use crate::error::ApiError;
use crate::state::AppState;

static REQUEST_COUNTER: AtomicU64 = AtomicU64::new(0);

/// POST /v1/chat/completions — render, tokenize, forward to the model-server,
/// and stream the tokens back.
pub async fn chat_completions(
    State(state): State<AppState>,
    Json(req): Json<ChatCompletionRequest>,
) -> Result<Response, ApiError> {
    let request_id = next_request_id();
    let created = unix_seconds();

    let messages: Vec<Message> = req.messages.iter().filter_map(to_message).collect();
    let inference = state
        .chat
        .prepare(&request_id, &messages)
        .map_err(|e| ApiError::Internal(e.to_string()))?;

    let input = GenerateInput {
        stage: RouteStage::Aggregate,
        request_id: request_id.clone(),
        prompt_token_ids: inference.prompt_token_ids,
        sampling_params: Default::default(),
        mm_features: None,
        arrival_time: None,
        cache_salt: None,
        trace_headers: None,
        priority: 0,
        data_parallel_rank: None,
        reasoning_parser_kwargs: None,
        lora_request: None,
        session_id: None,
    };

    // Collect the model-server response first; the mock response is tiny, and
    // true incremental SSE parsing is left for the real backend.
    let events = generate(&state, input).await?;

    if req.stream {
        let model = req.model;
        let total = events.len();
        let chat = state.chat.clone();
        let stream =
            futures::stream::iter(events.into_iter().enumerate().map(move |(i, event)| {
                let content = event_text(&chat, &event);
                let finish_reason =
                    (i + 1 == total && event_finished(&event)).then(|| "stop".to_string());
                let chunk = ChatCompletionChunk {
                    id: request_id.clone(),
                    object: "chat.completion.chunk".to_string(),
                    created,
                    model: model.clone(),
                    choices: vec![ChunkChoice {
                        index: 0,
                        delta: Delta {
                            role: (i == 0).then_some(ChatRole::Assistant),
                            content: Some(content),
                        },
                        finish_reason,
                    }],
                };
                let data = serde_json::to_string(&chunk).expect("chunk should serialize");
                Ok::<_, Infallible>(Event::default().data(data))
            }));
        Ok(Sse::new(stream)
            .keep_alive(KeepAlive::default())
            .into_response())
    } else {
        let token_ids: Vec<u32> = events.iter().flat_map(event_token_ids).collect();
        let content = state.chat.detokenize(&token_ids).unwrap_or_default();
        let finish_reason = events.last().and_then(event_finish_reason);
        let completion = ChatCompletion {
            id: request_id,
            object: "chat.completion".to_string(),
            created,
            model: req.model,
            choices: vec![CompletionChoice {
                index: 0,
                message: ChatMessage {
                    role: ChatRole::Assistant,
                    content,
                },
                finish_reason,
            }],
        };
        Ok(Json(completion).into_response())
    }
}

/// GET /health — liveness probe.
pub async fn health() -> StatusCode {
    StatusCode::OK
}

/// POST the tokenized request to the model-server and collect its SSE response.
async fn generate(state: &AppState, input: GenerateInput) -> Result<Vec<TokenEvent>, ApiError> {
    let response = reqwest::Client::new()
        .post(format!("{}/generate", state.model_server_url))
        .json(&input)
        .send()
        .await
        .map_err(|e| ApiError::Internal(e.to_string()))?;

    if !response.status().is_success() {
        return Err(ApiError::Internal(format!(
            "model-server returned {}",
            response.status()
        )));
    }

    let body = response
        .text()
        .await
        .map_err(|e| ApiError::Internal(e.to_string()))?;

    parse_sse(&body)
}

/// Parse an SSE body (`data: {json}\n\n`) into token events.
fn parse_sse(body: &str) -> Result<Vec<TokenEvent>, ApiError> {
    body.lines()
        .filter(|line| line.starts_with("data: "))
        .map(|line| {
            serde_json::from_str(&line["data: ".len()..])
                .map_err(|e| ApiError::Internal(e.to_string()))
        })
        .collect()
}

/// Convert an OpenAI chat message to the facade's message type. Tool messages
/// are dropped until the tool path is wired.
fn to_message(message: &ChatMessage) -> Option<Message> {
    let role = match message.role {
        ChatRole::System => Role::System,
        ChatRole::User => Role::User,
        ChatRole::Assistant => Role::Assistant,
        ChatRole::Tool => return None,
    };
    Some(Message {
        role,
        content: message.content.clone(),
    })
}

fn event_token_ids(event: &TokenEvent) -> Vec<u32> {
    match event {
        TokenEvent::Token(output) => output.token_ids.clone(),
        TokenEvent::Error { .. } => Vec::new(),
    }
}

fn event_text(chat: &ChatFacade, event: &TokenEvent) -> String {
    match event {
        TokenEvent::Token(output) => chat.detokenize(&output.token_ids).unwrap_or_default(),
        TokenEvent::Error { .. } => String::new(),
    }
}

fn event_finished(event: &TokenEvent) -> bool {
    matches!(event, TokenEvent::Token(output) if output.finish_reason.is_some())
}

fn event_finish_reason(event: &TokenEvent) -> Option<String> {
    match event {
        TokenEvent::Token(output) => output.finish_reason.as_ref().map(|_| "stop".to_string()),
        TokenEvent::Error { .. } => None,
    }
}

fn next_request_id() -> String {
    format!("req-{}", REQUEST_COUNTER.fetch_add(1, Ordering::Relaxed))
}

fn unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time before unix epoch")
        .as_secs()
}

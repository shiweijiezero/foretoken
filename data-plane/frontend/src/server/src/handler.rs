// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! HTTP handlers for the frontend routes.

use std::convert::Infallible;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::http::StatusCode;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use futures::StreamExt;

use crate::dto::{
    ChatCompletion, ChatCompletionChunk, ChatCompletionRequest, ChatMessage, ChatRole, ChunkChoice,
    CompletionChoice, Delta,
};
use crate::error::ApiError;
use crate::mock;

static REQUEST_COUNTER: AtomicU64 = AtomicU64::new(0);

/// POST /v1/chat/completions — returns a mock completion, streamed or not.
pub async fn chat_completions(
    Json(req): Json<ChatCompletionRequest>,
) -> Result<Response, ApiError> {
    let request_id = next_request_id();
    let created = unix_seconds();
    let tokens: Vec<String> = mock::mock_token_stream().collect().await;

    if req.stream {
        let model = req.model;
        let total = tokens.len();
        let stream =
            futures::stream::iter(tokens.into_iter().enumerate().map(move |(i, token)| {
                let chunk = ChatCompletionChunk {
                    id: request_id.clone(),
                    object: "chat.completion.chunk".to_string(),
                    created,
                    model: model.clone(),
                    choices: vec![ChunkChoice {
                        index: 0,
                        delta: Delta {
                            role: (i == 0).then_some(ChatRole::Assistant),
                            content: Some(token),
                        },
                        finish_reason: (i + 1 == total).then_some("stop".to_string()),
                    }],
                };
                let data = serde_json::to_string(&chunk).expect("chunk should serialize");
                Ok::<_, Infallible>(Event::default().data(data))
            }));
        Ok(Sse::new(stream)
            .keep_alive(KeepAlive::default())
            .into_response())
    } else {
        let completion = ChatCompletion {
            id: request_id,
            object: "chat.completion".to_string(),
            created,
            model: req.model,
            choices: vec![CompletionChoice {
                index: 0,
                message: ChatMessage {
                    role: ChatRole::Assistant,
                    content: tokens.concat(),
                },
                finish_reason: Some("stop".to_string()),
            }],
        };
        Ok(Json(completion).into_response())
    }
}

/// GET /health — liveness probe.
pub async fn health() -> StatusCode {
    StatusCode::OK
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

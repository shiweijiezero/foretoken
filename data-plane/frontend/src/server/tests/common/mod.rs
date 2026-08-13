// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared test helpers for the frontend server.

use std::convert::Infallible;
use std::sync::{Arc, Mutex};

use axum::{
    Json, Router,
    extract::State,
    http::StatusCode,
    response::sse::{Event, Sse},
    routing::post,
};
use foretoken_model_protocol::{GenerateInput, TokenEvent, TokenOutput};
use futures::StreamExt;
use futures::stream;
use vllm_llm::FinishReason;

/// Records the requests a mock model-server receives.
#[derive(Default)]
pub struct MockModelServer {
    pub received_prompt_token_ids: Mutex<Vec<Vec<u32>>>,
}

/// Build a mock model-server router that echoes fixed tokens over SSE.
pub fn build_mock_router(state: Arc<MockModelServer>) -> Router {
    Router::new()
        .route("/generate", post(generate))
        .with_state(state)
}

async fn generate(
    State(state): State<Arc<MockModelServer>>,
    Json(input): Json<GenerateInput>,
) -> Result<Sse<impl futures::Stream<Item = Result<Event, Infallible>>>, StatusCode> {
    state
        .received_prompt_token_ids
        .lock()
        .unwrap()
        .push(input.prompt_token_ids.clone());

    let request_id = input.request_id.clone();
    let events = vec![
        token(&request_id, vec![100, 200], None),
        token(&request_id, vec![300], Some(FinishReason::Stop(None))),
    ];

    let stream = stream::iter(events)
        .map(|event| Ok(Event::default().data(serde_json::to_string(&event).unwrap())));

    Ok(Sse::new(stream))
}

fn token(request_id: &str, token_ids: Vec<u32>, finish_reason: Option<FinishReason>) -> TokenEvent {
    TokenEvent::Token(Box::new(TokenOutput {
        request_id: request_id.to_string(),
        prompt_token_ids: None,
        prompt_logprobs: None,
        token_ids,
        logprobs: None,
        cached_token_count: 0,
        finish_reason,
        kv_transfer_params: None,
        ec_transfer_params: None,
    }))
}

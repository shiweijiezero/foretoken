// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Client API adapters sharing one generation service and response-stream lifecycle.

mod messages;
mod openai;
mod responses;
mod stream;

use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::Router;
use foretoken_chat::{ChatRequest, ParserSelection};
use foretoken_text::Prompt;
use uuid::Uuid;

use crate::runtime::{GeneratedChat, Generation, GenerationError, GenerationRequest};

/// Immutable frontend services shared by inference and diagnostic handlers.
#[derive(Clone)]
pub(crate) struct ApiState {
    pub generation: Arc<dyn Generation>,
    pub models: Arc<dyn Fn() -> Vec<String> + Send + Sync>,
    pub stream_idle: Duration,
}

impl ApiState {
    /// Dispatches a lowered chat request with the original HTTP timing and parser intent.
    /// The caller owns the returned stream; the generation service owns routing and cancellation.
    async fn generate_chat(
        &self,
        model: String,
        chat: ChatRequest,
        include_reasoning: bool,
        timing: RequestTiming,
    ) -> Result<GeneratedChat, GenerationError> {
        let request = GenerationRequest {
            model,
            request_id: chat.request_id.clone(),
            prompt: Prompt::Text(String::new()),
            sampling_params: chat.sampling_params.clone(),
            decode_options: chat.decode_options.clone(),
            intermediate: chat.intermediate,
            priority: chat.priority,
            cache_salt: chat.cache_salt.clone(),
            session_id: chat.session_id.clone(),
            arrival_time: timing.arrival_time,
            started_at: timing.started_at,
            tool_call_parser: if chat.tool_context.parsing_enabled() {
                ParserSelection::Auto
            } else {
                ParserSelection::None
            },
            // Parsing reasoning is independent of exposing it in the public response.
            reasoning_parser: ParserSelection::Auto,
        };
        self.generation
            .generate_chat(request, chat, include_reasoning)
            .await
    }
}

/// The common timing origin captured before protocol conversion or completion fan-out.
struct RequestTiming {
    started_at: Instant,
    arrival_time: Option<f64>,
}

impl RequestTiming {
    fn now() -> Self {
        Self {
            started_at: Instant::now(),
            arrival_time: Some(vllm_llm::current_unix_timestamp_secs()),
        }
    }
}

/// Combines client endpoints without adding another state, middleware, or execution lifecycle.
pub(crate) fn router() -> Router<ApiState> {
    openai::router()
        .merge(messages::router())
        .merge(responses::router())
}

/// Creates the request identity used by the backend and the corresponding API response.
fn server_request_id(prefix: &str) -> String {
    format!("{prefix}-{}", Uuid::new_v4())
}

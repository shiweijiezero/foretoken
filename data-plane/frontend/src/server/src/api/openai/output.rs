// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shares response metadata, usage and terminal-state encoding between OpenAI response adapters.

use std::time::{SystemTime, UNIX_EPOCH};

use foretoken_engine_core_client::protocol::output::StopReason;
use foretoken_text::DecodedPositionLogprobs;
use serde::Serialize;
use vllm_llm::FinishReason as VllmFinishReason;

use crate::runtime::Generated;

mod chat;
mod completions;

pub(crate) use chat::{chat_collected, chat_stream_with_options};
pub(crate) use completions::{CompletionResponseOptions, text_collected_many, text_stream_many};

#[derive(Clone, Serialize)]
struct ResponseMetadata {
    id: String,
    created: u64,
    model: String,
}

impl ResponseMetadata {
    fn from_generated(generated: &Generated) -> Self {
        Self {
            id: generated.routed.routed_request.request.request_id.clone(),
            created: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs(),
            model: generated.routed.routed_request.decision.model.clone(),
        }
    }
}

#[derive(Serialize)]
struct Usage {
    prompt_tokens: usize,
    completion_tokens: usize,
    total_tokens: usize,
    prompt_tokens_details: PromptTokenUsageDetails,
}

#[derive(Serialize)]
struct PromptTokenUsageDetails {
    cached_tokens: usize,
}

impl Usage {
    fn from_counts(prompt_tokens: usize, completion_tokens: usize, cached_tokens: usize) -> Self {
        Self {
            prompt_tokens,
            completion_tokens,
            total_tokens: prompt_tokens + completion_tokens,
            prompt_tokens_details: PromptTokenUsageDetails { cached_tokens },
        }
    }
}

#[derive(Serialize)]
#[serde(untagged)]
enum OpenAiStopReason {
    TokenId(u32),
    Text(String),
}

#[derive(Serialize)]
struct StreamError<'a> {
    error: StreamErrorBody<'a>,
}

#[derive(Serialize)]
struct StreamErrorBody<'a> {
    message: &'a str,
    #[serde(rename = "type")]
    kind: &'a str,
    code: &'a str,
}

fn stream_backend_error() -> StreamError<'static> {
    StreamError {
        error: StreamErrorBody {
            message: "model server request failed",
            kind: "server_error",
            code: "request_failed",
        },
    }
}

fn openai_stop_reason(finish_reason: &VllmFinishReason) -> Option<OpenAiStopReason> {
    match finish_reason.as_stop_reason()? {
        StopReason::TokenId(token_id) => Some(OpenAiStopReason::TokenId(*token_id)),
        StopReason::Text(text) => Some(OpenAiStopReason::Text(text.clone())),
    }
}

fn selected_logprob(
    position: &DecodedPositionLogprobs,
    token_id: u32,
) -> Option<&foretoken_text::DecodedTokenLogprob> {
    position
        .entries
        .iter()
        .find(|entry| entry.token_id == token_id)
}

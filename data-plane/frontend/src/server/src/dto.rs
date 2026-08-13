// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! OpenAI-compatible request and response DTOs.

use serde::{Deserialize, Serialize};

/// Minimal subset of the OpenAI chat-completions request.
#[derive(Debug, Clone, Deserialize)]
pub struct ChatCompletionRequest {
    pub model: String,
    pub messages: Vec<ChatMessage>,
    #[serde(default)]
    pub stream: bool,
}

/// One chat message.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ChatMessage {
    pub role: ChatRole,
    pub content: String,
}

/// Message author role.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ChatRole {
    System,
    User,
    Assistant,
    Tool,
}

/// Streamed chat-completion chunk (SSE `data:` payload).
#[derive(Debug, Clone, Serialize)]
pub struct ChatCompletionChunk {
    pub id: String,
    pub object: String,
    pub created: u64,
    pub model: String,
    pub choices: Vec<ChunkChoice>,
}

/// One choice inside a streamed chunk.
#[derive(Debug, Clone, Serialize)]
pub struct ChunkChoice {
    pub index: u32,
    pub delta: Delta,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<String>,
}

/// Incremental content carried by one streamed chunk.
#[derive(Debug, Clone, Serialize)]
pub struct Delta {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub role: Option<ChatRole>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
}

/// Non-streamed chat completion.
#[derive(Debug, Clone, Serialize)]
pub struct ChatCompletion {
    pub id: String,
    pub object: String,
    pub created: u64,
    pub model: String,
    pub choices: Vec<CompletionChoice>,
}

/// One choice inside a non-streamed completion.
#[derive(Debug, Clone, Serialize)]
pub struct CompletionChoice {
    pub index: u32,
    pub message: ChatMessage,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<String>,
}

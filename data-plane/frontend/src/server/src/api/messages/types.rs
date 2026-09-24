// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the vLLM project
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Anthropic wire types adapted from vLLM PR #52896, commit
//! 1e19c0826853371a5549f23d83678b7e56b8baea, routes/anthropic/types.rs.

use std::collections::HashMap;

use foretoken_chat::ReasoningEffort;
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Generation input consumed by the Messages handler.
#[derive(Debug, Deserialize)]
pub(super) struct AnthropicMessagesRequest {
    pub model: String,
    pub messages: Vec<AnthropicMessage>,
    pub max_tokens: u32,
    pub system: Option<SystemPrompt>,
    pub stop_sequences: Option<Vec<String>>,
    #[serde(default)]
    pub stream: bool,
    pub temperature: Option<f32>,
    pub top_p: Option<f32>,
    pub top_k: Option<u32>,
    pub tools: Option<Vec<AnthropicTool>>,
    pub tool_choice: Option<AnthropicToolChoice>,
    pub output_config: Option<AnthropicOutputConfig>,
    pub thinking: Option<Thinking>,
    pub cache_salt: Option<String>,
    pub chat_template_kwargs: Option<HashMap<String, Value>>,
    // Transfer state belongs to Foretoken's workflow, not a public client.
    pub kv_transfer_params: Option<Value>,
    pub ec_transfer_params: Option<Value>,
    pub context_management: Option<Value>,
    pub container: Option<Value>,
    pub mcp_servers: Option<Value>,
}

/// Prompt-shaping input for exact model-template token counting.
#[derive(Debug, Deserialize)]
pub(super) struct AnthropicCountTokensRequest {
    pub model: String,
    pub messages: Vec<AnthropicMessage>,
    pub system: Option<SystemPrompt>,
    pub tools: Option<Vec<AnthropicTool>>,
    pub tool_choice: Option<AnthropicToolChoice>,
    pub thinking: Option<Thinking>,
    pub output_config: Option<AnthropicOutputConfig>,
    pub chat_template_kwargs: Option<HashMap<String, Value>>,
}

/// One ordered Anthropic history message.
#[derive(Debug, Deserialize)]
pub(super) struct AnthropicMessage {
    pub role: AnthropicRole,
    pub content: MessageContent,
}

/// Input blocks retain text, reasoning and tool boundaries during lowering.
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum AnthropicContentBlock {
    Text {
        text: String,
    },
    Image {
        source: ImageSource,
    },
    Thinking {
        thinking: String,
        signature: Option<String>,
    },
    RedactedThinking {
        data: String,
    },
    ToolUse {
        id: String,
        name: String,
        input: Value,
    },
    ToolResult {
        tool_use_id: String,
        content: Option<ToolResultContent>,
        is_error: Option<bool>,
    },
    ToolReference {
        tool_name: String,
    },
}

/// A client-owned tool; versioned server tools are distinguished by `type`.
#[derive(Debug, Deserialize)]
pub(super) struct AnthropicTool {
    #[serde(rename = "type")]
    pub tool_type: Option<String>,
    pub name: String,
    pub description: Option<String>,
    pub input_schema: Option<Value>,
    pub strict: Option<bool>,
    #[serde(default)]
    pub defer_loading: bool,
}

/// Anthropic tool choice and parallel-use semantics.
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub(super) enum AnthropicToolChoice {
    Auto {
        disable_parallel_tool_use: Option<bool>,
    },
    Any {
        disable_parallel_tool_use: Option<bool>,
    },
    Tool {
        name: String,
        disable_parallel_tool_use: Option<bool>,
    },
    None,
}

/// Structured-output format and model reasoning effort.
#[derive(Debug, Deserialize)]
pub(super) struct AnthropicOutputConfig {
    pub format: Option<OutputFormat>,
    pub effort: Option<Effort>,
}

/// Top-level system prompt in the Anthropic wire format.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(super) enum SystemPrompt {
    Text(String),
    Blocks(Vec<SystemTextBlock>),
}

/// System content accepts text blocks only.
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum SystemTextBlock {
    Text { text: String },
}

/// Messages roles, including vLLM's inline-system extension.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub(super) enum AnthropicRole {
    User,
    Assistant,
    System,
}

/// Text shorthand or ordered typed content blocks.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(super) enum MessageContent {
    Text(String),
    Blocks(Vec<AnthropicContentBlock>),
}

/// Image locations retain their wire type for capability validation.
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum ImageSource {
    Base64 { media_type: String, data: String },
    Url { url: String },
}

/// A tool result may contain text or ordered multimodal content.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(super) enum ToolResultContent {
    Text(String),
    Blocks(Vec<AnthropicContentBlock>),
}

/// JSON-schema response constraint.
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum OutputFormat {
    JsonSchema { schema: Value },
}

/// Effort levels exposed by the Anthropic endpoint.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub(super) enum Effort {
    Low,
    Medium,
    High,
    XHigh,
    Max,
}

impl From<Effort> for ReasoningEffort {
    fn from(effort: Effort) -> Self {
        match effort {
            Effort::Low => Self::Low,
            Effort::Medium => Self::Medium,
            Effort::High => Self::High,
            Effort::XHigh => Self::XHigh,
            Effort::Max => Self::Max,
        }
    }
}

/// Thinking mode; a separate numeric budget needs backend enforcement.
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub(super) enum Thinking {
    Enabled {
        budget_tokens: Option<u32>,
        display: Option<ThinkingDisplay>,
    },
    Disabled,
    Adaptive {
        display: Option<ThinkingDisplay>,
    },
}

/// Controls reasoning visibility without disabling model reasoning.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub(super) enum ThinkingDisplay {
    Summarized,
    Omitted,
}

/// Completed Anthropic message, also used by the streaming start envelope.
#[derive(Debug, Serialize)]
pub(super) struct AnthropicMessagesResponse {
    pub id: String,
    #[serde(rename = "type")]
    pub response_type: &'static str,
    pub role: &'static str,
    pub model: String,
    pub content: Vec<ResponseContentBlock>,
    pub stop_reason: Option<StopReason>,
    pub stop_sequence: Option<String>,
    pub usage: AnthropicUsage,
}

/// Token accounting excludes cached input from newly processed input tokens.
#[derive(Debug, Serialize)]
pub(super) struct AnthropicUsage {
    pub input_tokens: usize,
    pub output_tokens: usize,
    pub cache_read_input_tokens: usize,
}

/// Ordered blocks encoded in a completed Messages response.
#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum ResponseContentBlock {
    Text {
        text: String,
    },
    Thinking {
        thinking: String,
    },
    ToolUse {
        id: String,
        name: String,
        input: Value,
    },
}

/// Successful terminal causes supported by Messages.
#[derive(Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum StopReason {
    EndTurn,
    MaxTokens,
    StopSequence,
    ToolUse,
}

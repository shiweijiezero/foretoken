// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the vLLM project
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Responses wire types adapted from vLLM PR #53380, commit
//! 9de2bc119009c3e37e36ddbe240c5647d848f752.
//! Foretoken keeps execution and request lifecycle outside these protocol types.
//!
//! Request types mirror the Python vLLM `ResponsesRequest` class in
//! `vllm/entrypoints/openai/responses/protocol.py`; output item and event
//! types mirror the `openai.types.responses` SDK shapes emitted by the Python
//! frontend.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use foretoken_chat::ReasoningEffort;

/// Responses API `input` field: either a plain string (single user message)
/// or a list of input/output items.
///
/// Items stay as raw JSON values at this layer so that conversion in
/// `convert.rs` can normalize legacy shapes (e.g. type-less messages) and
/// report precise per-item errors, mirroring the Python `input_item_parsing`
/// validator.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ResponsesInput {
    /// Simple text input; rendered as one user message.
    Text(String),
    /// Ordered list of input/output items (messages, function calls,
    /// function call outputs, reasoning items, ...).
    Items(Vec<Value>),
}

/// Responses API reasoning configuration.
///
/// The automatic summary setting exposes model reasoning without running a second model.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ResponsesReasoning {
    #[serde(default)]
    pub effort: Option<ReasoningEffort>,
    #[serde(default)]
    pub summary: Option<String>,
}

/// Responses API `text.format` variants.
///
/// Mirrors the `ResponseTextConfig.format` union from the OpenAI SDK. Note
/// the `json_schema` variant is flat here (`type` next to `name`/`schema`),
/// unlike the chat-completions shape where the schema is nested under a
/// `json_schema` key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ResponseTextFormat {
    Text,
    JsonObject,
    JsonSchema {
        name: String,
        #[serde(default)]
        description: Option<String>,
        schema: Value,
        #[serde(default)]
        strict: Option<bool>,
    },
}

/// Responses API `text` configuration.
///
/// Unsupported verbosity directives are rejected during lowering.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ResponseTextConfig {
    #[serde(default)]
    pub format: Option<ResponseTextFormat>,
    #[serde(default)]
    pub verbosity: Option<String>,
}

/// Responses API `tool_choice` field.
///
/// The string form covers `none`/`auto`/`required`; the object form is kept
/// as raw JSON so `convert.rs` can distinguish supported
/// (`{"type": "function", "name": ...}`) from unsupported object choices and
/// report precise errors.

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ResponseToolChoice {
    /// `none` / `auto` / `required`.
    Mode(String),
    /// Object form, e.g. `{"type": "function", "name": "..."}`.
    Object(Value),
}

/// Responses API request body accepted by the Rust frontend.
///
/// Semantic fields unsupported by the shared runtime are rejected during lowering.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ResponsesRequest {
    /// Public model identity; required by request lowering.
    #[serde(default)]
    pub model: Option<String>,
    /// Request input: a string or an ordered item list.
    pub input: ResponsesInput,
    /// Unsupported semantic fields are rejected instead of silently discarded.
    #[serde(flatten)]
    pub extra: serde_json::Map<String, Value>,
    /// System-level instructions prepended to the conversation.
    #[serde(default)]
    pub instructions: Option<String>,
    /// Function tools available to the model. Built-in tool types
    /// (web search, code interpreter, MCP, ...) are not supported and are
    /// rejected during conversion.
    #[serde(default)]
    pub tools: Vec<Value>,
    /// Tool selection behavior.
    #[serde(default)]
    pub tool_choice: Option<ResponseToolChoice>,
    #[serde(default)]
    pub parallel_tool_calls: Option<bool>,
    #[serde(default)]
    pub max_output_tokens: Option<u32>,
    #[serde(default)]
    pub max_tool_calls: Option<u32>,
    #[serde(default)]
    pub metadata: Option<Value>,
    #[serde(default)]
    pub previous_response_id: Option<String>,
    #[serde(default)]
    pub prompt: Option<Value>,
    #[serde(default)]
    pub reasoning: Option<ResponsesReasoning>,
    /// Whether to include reasoning content in the response. When false,
    /// reasoning tokens are still generated but excluded from the final
    /// output items.
    #[serde(default = "default_true")]
    pub include_reasoning: bool,
    #[serde(default)]
    pub service_tier: Option<String>,
    #[serde(default)]
    pub store: Option<bool>,
    #[serde(default)]
    pub background: Option<bool>,
    #[serde(default)]
    pub stream: bool,
    #[serde(default)]
    pub temperature: Option<f32>,
    #[serde(default)]
    pub top_p: Option<f32>,
    #[serde(default)]
    pub top_k: Option<u32>,
    #[serde(default)]
    pub top_logprobs: Option<i32>,
    #[serde(default)]
    pub text: Option<ResponseTextConfig>,
    #[serde(default)]
    pub truncation: Option<String>,
    #[serde(default)]
    pub user: Option<String>,
    #[serde(default)]
    pub include: Option<Vec<String>>,
    #[serde(default)]
    pub presence_penalty: Option<f32>,
    #[serde(default)]
    pub frequency_penalty: Option<f32>,
    #[serde(default)]
    pub repetition_penalty: Option<f32>,
    #[serde(default)]
    pub seed: Option<i64>,
    #[serde(default)]
    pub stop: Option<Value>,
    #[serde(default)]
    pub ignore_eos: bool,
    #[serde(default = "default_true")]
    pub skip_special_tokens: bool,
    #[serde(default)]
    pub include_stop_str_in_output: bool,
    #[serde(default)]
    pub min_tokens: Option<u32>,
    #[serde(default)]
    pub logit_bias: Option<HashMap<String, f32>>,
    #[serde(default)]
    pub stop_token_ids: Option<Vec<u32>>,

    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub priority: Option<i32>,
    #[serde(default)]
    pub cache_salt: Option<String>,
    #[serde(default)]
    pub chat_template_kwargs: Option<HashMap<String, Value>>,
    #[serde(default)]
    pub structured_outputs: Option<Value>,
    #[serde(default)]
    pub kv_transfer_params: Option<HashMap<String, Value>>,
    #[serde(default)]
    pub ec_transfer_params: Option<HashMap<String, Value>>,
    #[serde(default)]
    pub vllm_xargs: Option<HashMap<String, Value>>,
}

fn default_true() -> bool {
    true
}

/// One typed input/output item, parsed from the raw JSON in
/// [`ResponsesInput::Items`] during conversion.
///
/// Variants cover the supported Responses API history surface: messages,
/// function calls, function call outputs, and reasoning items. Custom-text
/// tool calls and outputs are normalized to function items before parsing.
/// Item types requiring server-side state or unsupported modalities
/// (`item_reference`, `mcp_call`, file inputs, ...) are rejected explicitly.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum ResponseInputItem {
    /// Chat-style message item (`EasyInputMessageParam` or
    /// `ResponseOutputMessage` shapes both land here; `convert.rs` inserts
    /// `type: "message"` when absent, mirroring the Python validator).
    Message(ResponseInputMessage),
    FunctionCall(ResponseInputFunctionCall),
    FunctionCallOutput(ResponseInputFunctionCallOutput),
    Reasoning(ResponseInputReasoning),
}

/// One message-shaped input item.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub(super) struct ResponseInputMessage {
    pub role: String,
    pub content: ResponseMessageContent,
    /// `in_progress`/`incomplete` on the final assistant item requests a
    /// partial-completion continuation (see `should_continue_final_message`
    /// in the Python frontend).
    #[serde(default)]
    pub status: Option<String>,
}

/// Message content: either a plain string or a list of typed parts.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub(super) enum ResponseMessageContent {
    Text(String),
    Parts(Vec<ResponseInputContentPart>),
}

/// One message content part.
///
/// `output_text`/`refusal` appear on assistant history items echoed back by
/// clients; the rest appear on user/system/developer inputs. File inputs are
/// rejected during conversion.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum ResponseInputContentPart {
    InputText {
        text: String,
    },
    InputImage {
        #[serde(default)]
        image_url: Option<String>,
        #[serde(default)]
        detail: Option<Value>,
        #[serde(default)]
        file_id: Option<String>,
    },
    InputAudio {
        data: String,
        #[serde(default)]
        format: Option<String>,
    },
    InputFile {
        #[serde(flatten)]
        extra: serde_json::Map<String, Value>,
    },
    OutputText {
        text: String,
        #[serde(default)]
        annotations: Option<Value>,
    },
    Refusal {
        refusal: String,
    },
}

/// One `function_call` input item (assistant tool invocation in history).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub(super) struct ResponseInputFunctionCall {
    pub call_id: String,
    pub name: String,
    pub arguments: String,
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
}

/// One `function_call_output` input item (tool result in history).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub(super) struct ResponseInputFunctionCallOutput {
    pub call_id: String,
    pub output: ResponseMessageContent,
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
}

/// One `reasoning` input item (assistant reasoning in history).
///
/// Public reasoning text can be replayed. Nonempty opaque encrypted history
/// is rejected because this adapter does not own its decryption.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub(super) struct ResponseInputReasoning {
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub summary: Option<Vec<TextPart>>,
    #[serde(default)]
    pub content: Option<Vec<TextPart>>,
    #[serde(default)]
    pub encrypted_content: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
}

/// One plain-text part used by reasoning items.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum TextPart {
    ReasoningText { text: String },
    SummaryText { text: String },
}

impl TextPart {
    /// Construct one `reasoning_text` part with the given text.
    pub fn reasoning_text(text: String) -> Self {
        Self::ReasoningText { text }
    }

    /// Return the carried text.
    pub fn text(&self) -> &str {
        match self {
            Self::ReasoningText { text } | Self::SummaryText { text } => text,
        }
    }
}

/// Status of a response object or one of its output items.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResponseItemStatus {
    Queued,
    InProgress,
    Incomplete,
    Failed,
    Cancelling,
    Cancelled,
    Completed,
}

/// One `output_text` content part of an output message item.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ResponseOutputContentPart {
    OutputText {
        text: String,
        annotations: Vec<Value>,
        /// Output logprobs are not exposed by this adapter.
        #[serde(skip)]
        logprobs: Option<Vec<Value>>,
    },
    Refusal {
        refusal: String,
    },
}

/// One item in the `output` array of a response (or streamed through
/// `response.output_item.*` events).
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ResponseOutputItem {
    Message {
        id: String,
        role: AssistantRole,
        status: ResponseItemStatus,
        content: Vec<ResponseOutputContentPart>,
    },
    FunctionCall {
        id: String,
        call_id: String,
        name: String,
        arguments: String,
        #[serde(default)]
        status: Option<ResponseItemStatus>,
    },
    Reasoning {
        id: String,
        summary: Vec<TextPart>,
        #[serde(default)]
        content: Option<Vec<TextPart>>,
        #[serde(default)]
        status: Option<ResponseItemStatus>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AssistantRole;

impl serde::Serialize for AssistantRole {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_str("assistant")
    }
}

/// Usage block of a completed response.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResponseUsage {
    pub input_tokens: usize,
    pub input_tokens_details: InputTokensDetails,
    pub output_tokens: usize,
    pub output_tokens_details: OutputTokensDetails,
    pub total_tokens: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct InputTokensDetails {
    pub cached_tokens: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OutputTokensDetails {
    pub reasoning_tokens: usize,
}

/// `incomplete_details` block set when generation stopped early.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IncompleteDetails {
    /// Currently only `max_output_tokens` is produced; `content_filter` is
    /// not implemented (same as the Python frontend).
    pub reason: String,
}

/// The top-level response object returned by non-streaming requests and
/// carried by lifecycle streaming events.
///
/// Sampling overrides are echoed only when supplied; omitted values remain
/// model-owned rather than being replaced by protocol defaults.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ResponsesResponse {
    pub id: String,
    pub object: ResponseObject,
    pub created_at: u64,
    pub status: ResponseItemStatus,
    pub background: bool,
    pub store: bool,
    pub error: Option<Value>,
    #[serde(default)]
    pub incomplete_details: Option<IncompleteDetails>,
    #[serde(default)]
    pub instructions: Option<String>,
    #[serde(default)]
    pub max_output_tokens: Option<u32>,
    #[serde(default)]
    pub max_tool_calls: Option<u32>,
    #[serde(default)]
    pub metadata: Option<Value>,
    pub model: String,
    pub output: Vec<ResponseOutputItem>,
    pub parallel_tool_calls: bool,
    #[serde(default)]
    pub previous_response_id: Option<String>,
    #[serde(default)]
    pub prompt: Option<Value>,
    #[serde(default)]
    pub reasoning: Option<ResponsesReasoning>,
    pub service_tier: String,
    pub temperature: Option<f32>,
    #[serde(default)]
    pub text: Option<ResponseTextConfig>,
    /// Echoed resolved tool choice. Raw JSON so named function choices round
    /// trip verbatim.
    pub tool_choice: Value,
    /// Echoed request tools (raw JSON, as received).
    pub tools: Vec<Value>,
    pub top_p: Option<f32>,
    #[serde(default)]
    pub top_logprobs: Option<i32>,
    pub truncation: String,
    #[serde(default)]
    pub usage: Option<ResponseUsage>,
    #[serde(default)]
    pub user: Option<String>,
    #[serde(default)]
    pub presence_penalty: Option<f32>,
    #[serde(default)]
    pub frequency_penalty: Option<f32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub struct ResponseObject;

impl serde::Serialize for ResponseObject {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_str("response")
    }
}

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! OpenAI request and tokenization wire types consumed by the HTTP adapters.

use std::collections::HashMap;

use foretoken_chat::ReasoningEffort;
use foretoken_text::Prompt;
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(super) enum TokenizeRequest {
    Completion(TokenizeCompletionRequest),
    Chat(TokenizeChatRequest),
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct TokenizeCompletionRequest {
    #[serde(default)]
    pub(super) model: Option<String>,
    pub(super) prompt: String,
    #[serde(default = "default_true")]
    pub(super) add_special_tokens: bool,
    #[serde(default)]
    pub(super) return_token_strs: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct TokenizeChatRequest {
    #[serde(default)]
    pub(super) model: Option<String>,
    pub(super) messages: Vec<OpenAiMessage>,
    #[serde(default = "default_true")]
    pub(super) add_generation_prompt: bool,
    #[serde(default)]
    pub(super) continue_final_message: bool,
    #[serde(default)]
    pub(super) add_special_tokens: bool,
    #[serde(default)]
    pub(super) return_token_strs: bool,
    #[serde(default)]
    pub(super) tools: Vec<OpenAiTool>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct DetokenizeRequest {
    #[serde(default)]
    pub(super) model: Option<String>,
    pub(super) tokens: Vec<u32>,
}

#[derive(Serialize)]
pub(super) struct TokenizeResponse {
    pub(super) count: usize,
    pub(super) max_model_len: u32,
    pub(super) tokens: Vec<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(super) token_strs: Option<Vec<String>>,
}

#[derive(Serialize)]
pub(super) struct DetokenizeResponse {
    pub(super) prompt: String,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(super) enum CompletionPrompt {
    Text(String),
    TokenIds(Vec<u32>),
    Texts(Vec<String>),
    TokenIdBatches(Vec<Vec<u32>>),
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct CompletionRequest {
    pub(super) model: String,
    pub(super) prompt: CompletionPrompt,
    #[serde(default)]
    pub(super) stream: bool,
    #[serde(default)]
    pub(super) stream_options: OpenAiStreamOptions,
    #[serde(default = "one")]
    pub(super) n: u32,
    #[serde(default)]
    pub(super) best_of: Option<u32>,
    #[serde(default)]
    pub(super) echo: bool,
    #[serde(default)]
    pub(super) return_token_ids: bool,
    #[serde(default)]
    pub(super) return_tokens_as_token_ids: bool,
    #[serde(default)]
    pub(super) return_prompt_token_ids: bool,
    #[serde(default, rename = "request_id")]
    _client_request_id: Option<String>,
    #[serde(default)]
    pub(super) temperature: Option<f32>,
    #[serde(default)]
    pub(super) top_p: Option<f32>,
    #[serde(default)]
    pub(super) top_k: Option<u32>,
    #[serde(default)]
    pub(super) min_p: Option<f32>,
    #[serde(default)]
    pub(super) seed: Option<i64>,
    #[serde(default)]
    pub(super) max_tokens: Option<u32>,
    #[serde(default)]
    pub(super) min_tokens: Option<u32>,
    #[serde(default)]
    pub(super) frequency_penalty: Option<f32>,
    #[serde(default)]
    pub(super) presence_penalty: Option<f32>,
    #[serde(default)]
    pub(super) repetition_penalty: Option<f32>,
    #[serde(default)]
    pub(super) stop_token_ids: Option<Vec<u32>>,
    #[serde(default)]
    pub(super) ignore_eos: bool,
    #[serde(default)]
    pub(super) logit_bias: Option<HashMap<u32, f32>>,
    #[serde(default)]
    pub(super) allowed_token_ids: Option<Vec<u32>>,
    #[serde(default)]
    pub(super) bad_words: Option<Vec<String>>,
    #[serde(default)]
    pub(super) logprobs: Option<i32>,
    #[serde(default)]
    pub(super) prompt_logprobs: Option<i32>,
    #[serde(default)]
    pub(super) priority: i32,
    #[serde(default)]
    pub(super) cache_salt: Option<String>,
    #[serde(default)]
    pub(super) session_id: Option<String>,
    #[serde(default)]
    pub(super) stop: Option<Stop>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OpenAiStreamOptions {
    #[serde(default)]
    pub(super) include_usage: bool,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(untagged)]
pub(super) enum Stop {
    One(String),
    Many(Vec<String>),
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ChatCompletionRequest {
    pub(super) model: String,
    pub(super) messages: Vec<Value>,
    #[serde(default)]
    pub(super) store: Option<bool>,
    #[serde(default)]
    pub(super) metadata: Option<Value>,
    #[serde(default)]
    pub(super) stream: bool,
    #[serde(default)]
    pub(super) stream_options: OpenAiStreamOptions,
    #[serde(default, rename = "request_id")]
    _client_request_id: Option<String>,
    #[serde(default)]
    pub(super) temperature: Option<f32>,
    #[serde(default)]
    pub(super) top_p: Option<f32>,
    #[serde(default)]
    pub(super) top_k: Option<u32>,
    #[serde(default)]
    pub(super) min_p: Option<f32>,
    #[serde(default)]
    pub(super) seed: Option<i64>,
    #[serde(default)]
    pub(super) max_tokens: Option<u32>,
    #[serde(default)]
    pub(super) min_tokens: Option<u32>,
    #[serde(default)]
    pub(super) frequency_penalty: Option<f32>,
    #[serde(default)]
    pub(super) presence_penalty: Option<f32>,
    #[serde(default)]
    pub(super) repetition_penalty: Option<f32>,
    #[serde(default)]
    pub(super) stop_token_ids: Option<Vec<u32>>,
    #[serde(default)]
    pub(super) ignore_eos: bool,
    #[serde(default)]
    pub(super) logit_bias: Option<HashMap<u32, f32>>,
    #[serde(default)]
    pub(super) allowed_token_ids: Option<Vec<u32>>,
    #[serde(default)]
    pub(super) bad_words: Option<Vec<String>>,
    #[serde(default)]
    pub(super) logprobs: bool,
    #[serde(default)]
    pub(super) top_logprobs: Option<u32>,
    #[serde(default)]
    pub(super) priority: i32,
    #[serde(default)]
    pub(super) cache_salt: Option<String>,
    #[serde(default)]
    pub(super) session_id: Option<String>,
    #[serde(default)]
    pub(super) stop: Option<Stop>,
    #[serde(default)]
    pub(super) tools: Vec<OpenAiTool>,
    #[serde(default)]
    pub(super) tool_choice: Option<OpenAiToolChoice>,
    #[serde(default)]
    pub(super) parallel_tool_calls: bool,
    #[serde(default)]
    pub(super) response_format: Option<OpenAiResponseFormat>,
    #[serde(default)]
    pub(super) reasoning_effort: Option<ReasoningEffort>,
    #[serde(default)]
    pub(super) include_reasoning: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OpenAiMessage {
    pub(super) role: String,
    #[serde(default)]
    pub(super) content: Option<OpenAiChatContent>,
    #[serde(default)]
    pub(super) tool_calls: Vec<OpenAiToolCall>,
    #[serde(default)]
    pub(super) tool_call_id: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(super) enum OpenAiChatContent {
    Text(String),
    Parts(Vec<OpenAiContentPart>),
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum OpenAiContentPart {
    Text { text: String },
    ImageUrl { image_url: OpenAiImageUrl },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OpenAiImageUrl {
    pub(super) url: String,
    #[serde(default)]
    pub(super) detail: Option<Value>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OpenAiToolCall {
    pub(super) id: String,
    #[serde(rename = "type")]
    pub(super) kind: OpenAiFunctionType,
    pub(super) function: OpenAiFunctionCall,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum OpenAiFunctionType {
    Function,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OpenAiFunctionCall {
    pub(super) name: String,
    pub(super) arguments: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OpenAiTool {
    #[serde(rename = "type")]
    pub(super) kind: OpenAiFunctionType,
    pub(super) function: OpenAiToolFunction,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OpenAiToolFunction {
    pub(super) name: String,
    #[serde(default)]
    pub(super) description: Option<String>,
    pub(super) parameters: Value,
    #[serde(default)]
    pub(super) strict: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(super) enum OpenAiToolChoice {
    Mode(OpenAiToolChoiceMode),
    Function(OpenAiNamedToolChoice),
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum OpenAiToolChoiceMode {
    None,
    Auto,
    Required,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OpenAiNamedToolChoice {
    #[serde(rename = "type")]
    pub(super) kind: OpenAiFunctionType,
    pub(super) function: OpenAiNamedFunction,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OpenAiNamedFunction {
    pub(super) name: String,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum OpenAiResponseFormat {
    Text,
    JsonObject,
    JsonSchema { json_schema: OpenAiJsonSchema },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OpenAiJsonSchema {
    pub(super) name: String,
    pub(super) schema: Value,
    #[serde(default)]
    pub(super) description: Option<String>,
    #[serde(default)]
    pub(super) strict: Option<bool>,
}

impl CompletionPrompt {
    /// Expands HTTP completion prompts into request-ordered generation inputs.
    pub(super) fn into_prompts(self) -> Vec<Prompt> {
        match self {
            Self::Text(text) => vec![Prompt::Text(text)],
            Self::TokenIds(token_ids) => vec![Prompt::TokenIds(token_ids)],
            Self::Texts(texts) => texts.into_iter().map(Prompt::Text).collect(),
            Self::TokenIdBatches(batches) => batches.into_iter().map(Prompt::TokenIds).collect(),
        }
    }
}

impl Stop {
    /// Normalizes completion stop strings for the shared request decoder.
    pub(super) fn into_vec(self) -> Vec<String> {
        match self {
            Self::One(s) => vec![s],
            Self::Many(v) => v,
        }
    }
}

fn default_true() -> bool {
    true
}

fn one() -> u32 {
    1
}

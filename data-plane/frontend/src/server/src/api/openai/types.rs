// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! OpenAI request and tokenization wire types consumed by the HTTP adapters.

use std::collections::HashMap;

pub(super) use async_openai::types::chat::{
    ChatCompletionMessageToolCalls as OpenAiToolCall,
    ChatCompletionRequestUserMessageContent as OpenAiChatContent,
    ChatCompletionRequestUserMessageContentPart as OpenAiContentPart,
    ChatCompletionToolChoiceOption as OpenAiToolChoice, ChatCompletionTools as OpenAiTool,
    Prompt as CompletionPrompt, ResponseFormat as OpenAiResponseFormat, StopConfiguration as Stop,
    ToolChoiceOptions as OpenAiToolChoiceMode,
};
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
    pub(super) max_completion_tokens: Option<u32>,
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

/// Supported message fields and reasoning history shared by generation and token counting.
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
    #[serde(default, alias = "reasoning_content")]
    pub(super) reasoning: Option<String>,
}

/// Expands standard completion prompts into request-ordered generation inputs.
pub(super) fn completion_prompts(prompt: CompletionPrompt) -> Vec<Prompt> {
    match prompt {
        CompletionPrompt::String(text) => vec![Prompt::Text(text)],
        CompletionPrompt::IntegerArray(token_ids) => vec![Prompt::TokenIds(token_ids)],
        CompletionPrompt::StringArray(texts) => texts.into_iter().map(Prompt::Text).collect(),
        CompletionPrompt::ArrayOfIntegerArray(batches) => {
            batches.into_iter().map(Prompt::TokenIds).collect()
        }
    }
}

fn default_true() -> bool {
    true
}

fn one() -> u32 {
    1
}

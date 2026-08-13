// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM-backed [`Renderer`] and [`Tokenizer`] implementations.
//!
//! This module is the only place vLLM types appear; the rest of the facade is
//! backend-agnostic.

use std::path::Path;

use vllm_chat::{ChatMessage, ChatRenderer, ChatRequest, ChatRole, DeepSeekV4ChatRenderer};
use vllm_text::Prompt;
use vllm_tokenizer::HuggingFaceTokenizer;
use vllm_tokenizer::Tokenizer as VllmTokenizer;

use crate::{ChatError, Message, Renderer, Role, Tokenizer};

/// DeepSeek V4 chat-template renderer backed by vLLM.
pub struct DeepSeekV4Renderer;

impl Renderer for DeepSeekV4Renderer {
    fn render(&self, messages: &[Message]) -> Result<String, ChatError> {
        let request = to_vllm_chat_request(messages);
        let rendered = DeepSeekV4ChatRenderer::new()
            .render(&request)
            .map_err(|e| ChatError::Render(e.to_string()))?;
        match rendered.prompt {
            Prompt::Text(text) => Ok(text),
            Prompt::TokenIds(_) => Err(ChatError::Render(
                "renderer produced token IDs instead of text".to_string(),
            )),
        }
    }
}

/// HuggingFace tokenizer backed by vLLM.
pub struct HfTokenizer(HuggingFaceTokenizer);

impl HfTokenizer {
    pub fn new(path: &Path) -> Result<Self, ChatError> {
        HuggingFaceTokenizer::new(path)
            .map(Self)
            .map_err(|e| ChatError::Tokenize(e.to_string()))
    }
}

impl Tokenizer for HfTokenizer {
    fn encode(&self, text: &str, add_special_tokens: bool) -> Result<Vec<u32>, ChatError> {
        self.0
            .encode(text, add_special_tokens)
            .map_err(|e| ChatError::Tokenize(e.to_string()))
    }

    fn decode(&self, ids: &[u32], skip_special_tokens: bool) -> Result<String, ChatError> {
        self.0
            .decode(ids, skip_special_tokens)
            .map_err(|e| ChatError::Decode(e.to_string()))
    }
}

fn to_vllm_chat_request(messages: &[Message]) -> ChatRequest {
    let mut request = ChatRequest::for_test();
    request.messages = messages.iter().map(to_vllm_message).collect();
    request
}

fn to_vllm_message(message: &Message) -> ChatMessage {
    let role = match message.role {
        Role::System => ChatRole::System,
        Role::User => ChatRole::User,
        Role::Assistant => ChatRole::Assistant,
    };
    ChatMessage::text(role, message.content.as_str())
}

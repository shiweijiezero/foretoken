// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Chat facade: render + tokenize chat messages into a backend-agnostic
//! inference request.
//!
//! The facade speaks only Foretoken's own traits ([`Renderer`], [`Tokenizer`])
//! and types ([`Message`], [`InferenceRequest`]); backend-specific types (vLLM,
//! SGLang, ...) live behind adapters such as [`vllm`].

use std::sync::Arc;

/// Renders structured chat messages into a single prompt string.
///
/// Rendering is model-specific but backend-agnostic: a given model family uses
/// the same chat template whether it runs on vLLM or SGLang.
pub trait Renderer: Send + Sync {
    fn render(&self, messages: &[Message]) -> Result<String, ChatError>;
}

/// Encodes/decodes between text and token IDs.
pub trait Tokenizer: Send + Sync {
    fn encode(&self, text: &str, add_special_tokens: bool) -> Result<Vec<u32>, ChatError>;
    fn decode(&self, ids: &[u32], skip_special_tokens: bool) -> Result<String, ChatError>;
}

/// One chat message in OpenAI-style role/content form.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Message {
    pub role: Role,
    pub content: String,
}

/// Message roles supported by the facade.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    System,
    User,
    Assistant,
}

/// A tokenized, backend-agnostic inference request.
pub struct InferenceRequest {
    pub request_id: String,
    pub prompt_token_ids: Vec<u32>,
}

/// Errors surfaced by the chat facade.
#[derive(Debug, thiserror::Error)]
pub enum ChatError {
    #[error("render failed: {0}")]
    Render(String),
    #[error("tokenize failed: {0}")]
    Tokenize(String),
    #[error("decode failed: {0}")]
    Decode(String),
}

/// Render + tokenize facade, decoupled from any concrete backend.
pub struct ChatFacade {
    renderer: Arc<dyn Renderer>,
    tokenizer: Arc<dyn Tokenizer>,
}

impl ChatFacade {
    pub fn new(renderer: Arc<dyn Renderer>, tokenizer: Arc<dyn Tokenizer>) -> Self {
        Self {
            renderer,
            tokenizer,
        }
    }

    /// Render the messages into a prompt, tokenize it, and assemble an
    /// inference request. Sampling lowering is left for a later step.
    pub fn prepare(
        &self,
        request_id: &str,
        messages: &[Message],
    ) -> Result<InferenceRequest, ChatError> {
        let prompt = self.renderer.render(messages)?;
        let prompt_token_ids = self.tokenizer.encode(&prompt, true)?;
        Ok(InferenceRequest {
            request_id: request_id.to_string(),
            prompt_token_ids,
        })
    }

    /// Decode token IDs back into text (for streaming responses).
    pub fn detokenize(&self, ids: &[u32]) -> Result<String, ChatError> {
        self.tokenizer.decode(ids, true)
    }
}

/// Backend adapters. vLLM-backed implementations live in [`vllm`].
pub mod vllm;

#[cfg(test)]
mod tests {
    use super::*;

    struct MockTokenizer;

    impl Tokenizer for MockTokenizer {
        fn encode(&self, text: &str, _add_special_tokens: bool) -> Result<Vec<u32>, ChatError> {
            Ok(text.chars().map(|c| c as u32).collect())
        }

        fn decode(&self, ids: &[u32], _skip_special_tokens: bool) -> Result<String, ChatError> {
            Ok(ids
                .iter()
                .map(|&id| char::from_u32(id).unwrap_or('?'))
                .collect())
        }
    }

    #[test]
    fn deepseek_v4_renders_and_tokenizes() {
        let facade = ChatFacade::new(Arc::new(vllm::DeepSeekV4Renderer), Arc::new(MockTokenizer));
        let messages = vec![Message {
            role: Role::User,
            content: "hi".to_string(),
        }];
        let request = facade.prepare("test-request", &messages).expect("prepare");
        assert_eq!(request.request_id, "test-request");
        assert!(!request.prompt_token_ids.is_empty());
    }

    #[test]
    fn detokenize_round_trips() {
        let facade = ChatFacade::new(Arc::new(vllm::DeepSeekV4Renderer), Arc::new(MockTokenizer));
        let text = facade.detokenize(&[104, 105]).expect("detokenize");
        assert_eq!(text, "hi");
    }
}

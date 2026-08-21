// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::Arc;

use foretoken_chat::{
    ChatBackend, ChatRenderer, ChatRequest, ChatRequestProcessor, DefaultChatOutputProcessor,
    DynChatOutputProcessor, DynChatRenderer, NewChatOutputProcessorOptions, RenderedPrompt,
};
use foretoken_server::{ModelRuntime, RuntimeBundle};
use foretoken_text::{Prompt, TextBackend, TextRequestProcessor};
use foretoken_tokenizer::{DynTokenizer, Result as TokenizerResult, Tokenizer};

struct TestTokenizer;

impl Tokenizer for TestTokenizer {
    fn encode(&self, text: &str, _: bool) -> TokenizerResult<Vec<u32>> {
        Ok(text.bytes().map(u32::from).collect())
    }

    fn encode_ordinary(&self, text: &str) -> TokenizerResult<Vec<u32>> {
        self.encode(text, false)
    }

    fn decode(&self, ids: &[u32], _: bool) -> TokenizerResult<String> {
        Ok(ids.iter().filter_map(|&id| char::from_u32(id)).collect())
    }

    fn token_to_id(&self, _: &str) -> Option<u32> {
        None
    }

    fn id_to_token(&self, id: u32) -> Option<String> {
        char::from_u32(id).map(|value| value.to_string())
    }
}

struct TestTextBackend(DynTokenizer);

impl TextBackend for TestTextBackend {
    fn tokenizer(&self) -> DynTokenizer {
        self.0.clone()
    }

    fn model_id(&self) -> &str {
        "test"
    }
}

struct TestRenderer;

impl ChatRenderer for TestRenderer {
    fn render(&self, _: &ChatRequest) -> foretoken_chat::Result<RenderedPrompt> {
        Ok(RenderedPrompt {
            prompt: Prompt::Text("test".into()),
            effective_template_kwargs: Default::default(),
        })
    }
}

struct TestChatBackend(DynTokenizer);

impl ChatBackend for TestChatBackend {
    fn chat_renderer(&self) -> DynChatRenderer {
        Arc::new(TestRenderer)
    }

    fn new_chat_output_processor(
        &self,
        request: &mut ChatRequest,
        options: NewChatOutputProcessorOptions<'_>,
    ) -> foretoken_chat::Result<DynChatOutputProcessor> {
        Ok(Box::new(DefaultChatOutputProcessor::new(
            request,
            "test",
            self.0.clone(),
            options.tool_call_parser,
            options.reasoning_parser,
        )?))
    }
}

pub fn test_runtime() -> ModelRuntime {
    let tokenizer: DynTokenizer = Arc::new(TestTokenizer);
    ModelRuntime::new(Arc::new(RuntimeBundle::new(
        Arc::new(TextRequestProcessor::new(
            Arc::new(TestTextBackend(tokenizer.clone())),
            1024,
        )),
        tokenizer.clone(),
        Arc::new(ChatRequestProcessor::render_only(Arc::new(
            TestChatBackend(tokenizer),
        ))),
    )))
}

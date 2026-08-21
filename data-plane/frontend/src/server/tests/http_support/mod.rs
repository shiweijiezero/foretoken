// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use foretoken_chat::{
    ChatRequest, DefaultChatOutputProcessor, DynChatOutputProcessor, ParserSelection,
};
use foretoken_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
use foretoken_model_protocol::ModelServerRole;
use foretoken_router::{RouteDecision, RouteTargetId, RouteTargetSet};
use foretoken_server::{
    Generated, GeneratedChat, Generation, GenerationError, GenerationRequest, KvIndexDiagnostics,
    RoutedGenerate, RoutedRequest, RuntimeDiagnostics, Tokenization, token_stream,
};
use foretoken_text::Prompt;
use foretoken_tokenizer::{DynTokenizer, Result as TokenizerResult, Tokenizer};
use vllm_llm::{FinishReason, GenerateOutput, GeneratePromptInfo, GenerateRequest};

pub type CapturedTextRequest = (
    Prompt,
    foretoken_text::SamplingParams,
    bool,
    i32,
    Option<String>,
    Option<String>,
    Option<f64>,
);

#[derive(Default)]
pub struct RecordingGeneration {
    pub calls: Mutex<Vec<(String, String)>>,
    pub request_ids: Mutex<Vec<String>>,
    pub text_requests: Mutex<Vec<CapturedTextRequest>>,
    pub chat_requests: Mutex<Vec<(ChatRequest, bool)>>,
}

#[async_trait]
impl Generation for RecordingGeneration {
    async fn generate(&self, request: GenerationRequest) -> Result<Generated, GenerationError> {
        self.text_requests.lock().unwrap().push((
            request.prompt.clone(),
            request.sampling_params.clone(),
            request.intermediate,
            request.priority,
            request.cache_salt.clone(),
            request.session_id.clone(),
            request.arrival_time,
        ));
        self.request_ids
            .lock()
            .unwrap()
            .push(request.request_id.clone());
        self.calls
            .lock()
            .unwrap()
            .push(("text".into(), request.request_id));
        Err(GenerationError::RequestFailed)
    }

    async fn generate_chat(
        &self,
        request: GenerationRequest,
        chat: ChatRequest,
        include_reasoning: bool,
    ) -> Result<GeneratedChat, GenerationError> {
        self.chat_requests
            .lock()
            .unwrap()
            .push((chat.clone(), include_reasoning));
        self.request_ids.lock().unwrap().push(request.request_id);
        self.calls
            .lock()
            .unwrap()
            .push(("chat".into(), chat.messages[0].text_content().unwrap()));
        Err(GenerationError::RequestFailed)
    }

    async fn tokenize(
        &self,
        _: &str,
        prompt: Prompt,
        _: bool,
        return_token_strs: bool,
    ) -> Result<Tokenization, GenerationError> {
        let token_ids = match prompt {
            Prompt::Text(text) => text.bytes().map(u32::from).collect(),
            Prompt::TokenIds(token_ids) => token_ids,
        };
        Ok(Tokenization {
            token_strs: return_token_strs.then(|| vec!["token".into(); token_ids.len()]),
            token_ids,
            max_model_len: 4096,
        })
    }

    async fn tokenize_chat(
        &self,
        _: &str,
        _: ChatRequest,
        return_token_strs: bool,
    ) -> Result<Tokenization, GenerationError> {
        Ok(Tokenization {
            token_ids: vec![1, 2],
            token_strs: return_token_strs.then(|| vec!["a".into(), "b".into()]),
            max_model_len: 4096,
        })
    }

    async fn detokenize(&self, _: &str, token_ids: &[u32]) -> Result<String, GenerationError> {
        Ok(token_ids
            .iter()
            .filter_map(|token_id| char::from_u32(*token_id))
            .collect())
    }

    fn ready(&self) -> bool {
        true
    }

    fn diagnostics(&self) -> RuntimeDiagnostics {
        RuntimeDiagnostics {
            serving_ready: true,
            active_generation: Some(7),
            kv_index: KvIndexDiagnostics {
                state: "degraded".into(),
                reason: Some("event_subscriber_unavailable".into()),
                sources_healthy: 1,
                sources_total: 2,
            },
        }
    }
}

struct ByteTokenizer;

impl Tokenizer for ByteTokenizer {
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

fn generated(request: GenerationRequest, tokenizer: DynTokenizer) -> Generated {
    let request_id = request.request_id;
    let model = request.model;
    Generated {
        routed: RoutedGenerate {
            routed_request: RoutedRequest {
                decision: RouteDecision {
                    route_target_id: RouteTargetId::new("group-a"),
                    admission_targets: RouteTargetSet::default(),
                    role: ModelServerRole::Aggregate,
                    model,
                    revision: "r1".into(),
                    data_parallel_rank: 0,
                },
                request: GenerateRequest {
                    request_id: request_id.clone(),
                    prompt_token_ids: vec![1],
                    sampling_params: EngineCoreSamplingParams::default(),
                    mm_features: None,
                    arrival_time: None,
                    cache_salt: None,
                    trace_headers: None,
                    priority: 0,
                    data_parallel_rank: None,
                    session_id: None,
                    reasoning_parser_kwargs: None,
                    lora_request: None,
                },
            },
            stream: token_stream(vec![
                Ok(GenerateOutput {
                    request_id: request_id.clone(),
                    prompt_info: Some(GeneratePromptInfo {
                        prompt_token_ids: vec![1].into(),
                        prompt_logprobs: None,
                    }),
                    token_ids: vec![b'h' as u32],
                    logprobs: None,
                    finish_reason: None,
                    cached_token_count: 0,
                    kv_transfer_params: None,
                    ec_transfer_params: None,
                }),
                Ok(GenerateOutput {
                    request_id,
                    prompt_info: None,
                    token_ids: vec![b'i' as u32],
                    logprobs: None,
                    finish_reason: Some(FinishReason::Length),
                    cached_token_count: 0,
                    kv_transfer_params: None,
                    ec_transfer_params: None,
                }),
            ]),
        },
        tokenizer,
        decode_options: request.decode_options,
    }
}

pub struct SuccessfulGeneration;

#[async_trait]
impl Generation for SuccessfulGeneration {
    async fn generate(&self, request: GenerationRequest) -> Result<Generated, GenerationError> {
        Ok(generated(request, Arc::new(ByteTokenizer)))
    }

    async fn generate_chat(
        &self,
        request: GenerationRequest,
        mut chat: ChatRequest,
        include_reasoning: bool,
    ) -> Result<GeneratedChat, GenerationError> {
        let tokenizer: DynTokenizer = Arc::new(ByteTokenizer);
        let tool_call_parser = ParserSelection::Auto;
        let reasoning_parser = ParserSelection::Auto;
        let output_processor: DynChatOutputProcessor = Box::new(
            DefaultChatOutputProcessor::new(
                &mut chat,
                "test",
                tokenizer.clone(),
                &tool_call_parser,
                &reasoning_parser,
            )
            .map_err(|_| GenerationError::Internal)?,
        );
        Ok(GeneratedChat {
            generated: generated(request, tokenizer),
            output_processor,
            include_reasoning,
        })
    }

    fn ready(&self) -> bool {
        true
    }
}

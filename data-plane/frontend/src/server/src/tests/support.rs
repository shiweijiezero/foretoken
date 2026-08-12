// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::{Arc, Mutex};

use async_trait::async_trait;

use crate::{
    Generated, GeneratedChat, Generation, GenerationError, GenerationRequest, KvIndexDiagnostics,
    ModelRuntime, RoutedGenerate, RoutedRequest, RuntimeBundle, RuntimeDiagnostics, RuntimeState,
    Tokenization, token_stream,
};
use foretoken_backend_registry::BackendRegistry;
use foretoken_chat::{
    ChatBackend, ChatRenderer, ChatRequest, ChatRequestProcessor, DefaultChatOutputProcessor,
    DynChatOutputProcessor, DynChatRenderer, NewChatOutputProcessorOptions, RenderedPrompt,
};
use foretoken_router::PipelineRouter;
use foretoken_text::{Prompt, TextBackend, TextRequestProcessor};
use foretoken_tokenizer::{DynTokenizer, Result as TokenizerResult, Tokenizer};

pub(super) type CapturedTextRequest = (
    Prompt,
    foretoken_text::SamplingParams,
    bool,
    i32,
    Option<String>,
    Option<f64>,
);

#[derive(Default)]
pub(super) struct RecordingGeneration {
    pub(super) calls: Mutex<Vec<(String, String)>>,
    pub(super) request_ids: Mutex<Vec<String>>,
    pub(super) text_requests: Mutex<Vec<CapturedTextRequest>>,
    pub(super) chat_requests: Mutex<Vec<(ChatRequest, bool)>>,
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

pub(super) struct TestTokenizer;

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

pub(super) struct TestTextBackend(DynTokenizer);

impl TextBackend for TestTextBackend {
    fn tokenizer(&self) -> DynTokenizer {
        self.0.clone()
    }

    fn model_id(&self) -> &str {
        "test"
    }
}

pub(super) struct TestRenderer;

impl ChatRenderer for TestRenderer {
    fn render(&self, _: &ChatRequest) -> foretoken_chat::Result<RenderedPrompt> {
        Ok(RenderedPrompt {
            prompt: Prompt::Text("test".into()),
            effective_template_kwargs: Default::default(),
        })
    }
}

pub(super) struct TestChatBackend(DynTokenizer);

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

pub(super) fn test_runtime(revision: &str) -> ModelRuntime {
    let tokenizer: DynTokenizer = Arc::new(TestTokenizer);
    ModelRuntime::new(
        revision.into(),
        Arc::new(RuntimeBundle::new(
            Arc::new(TextRequestProcessor::new(
                Arc::new(TestTextBackend(tokenizer.clone())),
                1024,
            )),
            tokenizer.clone(),
            Arc::new(ChatRequestProcessor::render_only(Arc::new(
                TestChatBackend(tokenizer),
            ))),
        )),
    )
}

pub(super) fn generation_state(model: &str, revision: &str) -> Arc<RuntimeState> {
    let snapshot = format!(
        r#"{{"version":1,"groups":[{{"service_uid":"service","pool_uid":"pool","pool_name":"pool","route_target_id":"a","model":"{model}","revision":"{revision}","tokenizer":"a","tokenizer_revision":"r1","endpoint":"http://127.0.0.1:1","data_parallel_size":1}}]}}"#
    );
    let registry = Arc::new(BackendRegistry::from_json(snapshot.as_bytes()).unwrap());
    Arc::new(RuntimeState::new(
        std::collections::BTreeMap::from([(model.into(), test_runtime(revision))]),
        Arc::new(PipelineRouter::new(registry.clone())),
        registry,
    ))
}

pub(super) fn generated_text(
    request_id: &str,
    model: &str,
    finish_reason: vllm_llm::FinishReason,
) -> Generated {
    use foretoken_model_protocol::ModelServerRole;
    use foretoken_router::{RouteDecision, RouteTargetId};
    use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
    use vllm_llm::{GenerateOutput, GeneratePromptInfo, GenerateRequest};

    let tokenizer: DynTokenizer = Arc::new(TestTokenizer);
    Generated {
        routed: RoutedGenerate {
            routed_request: RoutedRequest {
                decision: RouteDecision {
                    route_target_id: RouteTargetId::new("group-a"),
                    role: ModelServerRole::Aggregate,
                    model: model.into(),
                    revision: "r1".into(),
                    data_parallel_rank: 0,
                },
                request: GenerateRequest {
                    request_id: request_id.into(),
                    prompt_token_ids: vec![1],
                    sampling_params: EngineCoreSamplingParams::default(),
                    mm_features: None,
                    arrival_time: None,
                    cache_salt: None,
                    trace_headers: None,
                    priority: 0,
                    data_parallel_rank: None,
                    reasoning_parser_kwargs: None,
                    lora_request: None,
                },
            },
            stream: token_stream(vec![Ok(GenerateOutput {
                request_id: request_id.into(),
                prompt_info: Some(GeneratePromptInfo {
                    prompt_token_ids: vec![1].into(),
                    prompt_logprobs: None,
                }),
                token_ids: vec![b'h' as u32, b'i' as u32],
                logprobs: None,
                finish_reason: Some(finish_reason),
                cached_token_count: 0,
                kv_transfer_params: None,
                ec_transfer_params: None,
            })]),
        },
        tokenizer,
        decode_options: Default::default(),
    }
}

pub(super) fn generated_text_with_logprobs(
    request_id: &str,
    model: &str,
    finish_reason: vllm_llm::FinishReason,
) -> Generated {
    use vllm_llm::{GenerateOutput, GeneratePromptInfo, Logprobs, PositionLogprobs, TokenLogprob};

    let mut generated = generated_text(request_id, model, finish_reason.clone());
    generated.routed.stream = token_stream(vec![Ok(GenerateOutput {
        request_id: request_id.into(),
        prompt_info: Some(GeneratePromptInfo {
            prompt_token_ids: vec![1].into(),
            prompt_logprobs: None,
        }),
        token_ids: vec![b'h' as u32, b'i' as u32],
        logprobs: Some(Logprobs {
            positions: vec![
                PositionLogprobs {
                    entries: vec![
                        TokenLogprob {
                            token_id: b'h' as u32,
                            logprob: -0.1,
                            rank: 1,
                        },
                        TokenLogprob {
                            token_id: b'x' as u32,
                            logprob: -1.2,
                            rank: 2,
                        },
                    ],
                },
                PositionLogprobs {
                    entries: vec![TokenLogprob {
                        token_id: b'i' as u32,
                        logprob: -0.2,
                        rank: 1,
                    }],
                },
            ],
        }),
        finish_reason: Some(finish_reason),
        cached_token_count: 1,
        kv_transfer_params: None,
        ec_transfer_params: None,
    })]);
    generated
}

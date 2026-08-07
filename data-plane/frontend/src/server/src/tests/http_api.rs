use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use futures::stream;
use tower::ServiceExt;

use super::{
    CompletionResponseOptions, Generated, GeneratedChat, Generation, GenerationError,
    GenerationRequest, KvIndexDiagnostics, ModelRuntime, RoutedGenerate, RoutedRequest,
    RuntimeBundle, RuntimeControl, RuntimeDiagnostics, RuntimeGeneration, RuntimeState,
    Tokenization, chat_collected, idle_timed, router, stream_response, text_collected,
    text_collected_many, text_stream, text_stream_with_options, token_stream,
};
use foretoken_backend_registry::BackendRegistry;
use foretoken_chat::{
    ChatBackend, ChatMessage, ChatRenderer, ChatRequest, ChatRequestProcessor,
    DefaultChatOutputProcessor, DynChatOutputProcessor, DynChatRenderer,
    NewChatOutputProcessorOptions, RenderedPrompt,
};
use foretoken_router::{NeutralScorer, PolicyRouter};
use foretoken_text::{Prompt, TextBackend, TextRequestProcessor};
use foretoken_tokenizer::{DynTokenizer, Result as TokenizerResult, Tokenizer};

type CapturedTextRequest = (
    Prompt,
    foretoken_text::SamplingParams,
    bool,
    i32,
    Option<String>,
    Option<f64>,
);

#[derive(Default)]
struct RecordingGeneration {
    calls: Mutex<Vec<(String, String, String)>>,
    request_ids: Mutex<Vec<String>>,
    text_requests: Mutex<Vec<CapturedTextRequest>>,
    chat_requests: Mutex<Vec<(BTreeSet<String>, ChatRequest, bool)>>,
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
        self.calls.lock().unwrap().push((
            "text".into(),
            request.required_capabilities.iter().next().unwrap().clone(),
            request.request_id,
        ));
        Err(GenerationError::RequestFailed)
    }

    async fn generate_chat(
        &self,
        request: GenerationRequest,
        chat: ChatRequest,
        include_reasoning: bool,
    ) -> Result<GeneratedChat, GenerationError> {
        self.chat_requests.lock().unwrap().push((
            request.required_capabilities.clone(),
            chat.clone(),
            include_reasoning,
        ));
        self.request_ids.lock().unwrap().push(request.request_id);
        self.calls.lock().unwrap().push((
            "chat".into(),
            request.required_capabilities.iter().next().unwrap().clone(),
            chat.messages[0].text_content().unwrap(),
        ));
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

fn test_runtime(revision: &str) -> ModelRuntime {
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

fn generation_state(model: &str, revision: &str) -> Arc<RuntimeState> {
    let snapshot = format!(
        r#"{{"version":1,"groups":[{{"backend_id":"a","model":"{model}","revision":"{revision}","tokenizer":"a","tokenizer_revision":"r1","endpoint":"http://127.0.0.1:1"}}]}}"#
    );
    let registry = Arc::new(BackendRegistry::from_json(snapshot.as_bytes()).unwrap());
    Arc::new(RuntimeState::new(
        std::collections::BTreeMap::from([(model.into(), test_runtime(revision))]),
        Arc::new(PolicyRouter::new(registry.clone(), Arc::new(NeutralScorer))),
        registry,
    ))
}

fn generated_text(
    request_id: &str,
    model: &str,
    finish_reason: vllm_llm::FinishReason,
) -> Generated {
    use foretoken_router::{BackendId, RouteDecision};
    use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
    use vllm_llm::{GenerateOutput, GeneratePromptInfo, GenerateRequest};

    let tokenizer: DynTokenizer = Arc::new(TestTokenizer);
    Generated {
        routed: RoutedGenerate {
            routed_request: RoutedRequest {
                decision: RouteDecision {
                    backend_id: BackendId::new("group-a"),
                    model: model.into(),
                    revision: "r1".into(),
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

fn generated_text_with_logprobs(
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

struct StaticRuntimeControl {
    ready: bool,
    models: Vec<String>,
}

#[async_trait]
impl RuntimeControl for StaticRuntimeControl {
    async fn refresh_backend_readiness(&self) {}

    fn healthy_models(&self) -> Vec<String> {
        self.models.clone()
    }

    fn is_ready(&self) -> bool {
        self.ready
    }
}

#[test]
fn runtime_state_selects_model_and_requires_matching_revision() {
    let registry = Arc::new(
        BackendRegistry::from_json(
            br#"{"version":1,"groups":[{"backend_id":"a","model":"model-a","revision":"r1","tokenizer":"a","tokenizer_revision":"r1","endpoint":"http://127.0.0.1:1"}]}"#,
        )
        .unwrap(),
    );
    let state = RuntimeState::new(
        std::collections::BTreeMap::from([
            ("model-a".into(), test_runtime("r1")),
            ("model-b".into(), test_runtime("r2")),
        ]),
        Arc::new(PolicyRouter::new(registry.clone(), Arc::new(NeutralScorer))),
        registry,
    );

    assert_eq!(state.model("model-b", None).unwrap().revision(), "r2");
    assert!(state.model("model-b", Some("r1")).is_err());
    assert!(state.model("missing", None).is_err());
}

#[test]
fn runtime_generation_publishes_atomically_and_rejects_stale_versions() {
    let generation = RuntimeGeneration::new();
    assert!(!generation.ready());
    assert_eq!(generation.active_version(), None);

    assert!(generation.replace_state(
        2,
        generation_state("new", "r2"),
        Arc::new(StaticRuntimeControl {
            ready: true,
            models: vec!["new".into()],
        }),
    ));
    assert!(generation.ready());
    assert_eq!(generation.models(), vec!["new"]);
    assert_eq!(generation.active_version(), Some(2));

    assert!(!generation.replace_state(
        1,
        generation_state("old", "r1"),
        Arc::new(StaticRuntimeControl {
            ready: false,
            models: vec!["old".into()],
        }),
    ));
    assert!(generation.ready());
    assert_eq!(generation.models(), vec!["new"]);
    assert_eq!(generation.active_version(), Some(2));

    assert!(generation.replace_state(
        3,
        generation_state("next", "r3"),
        Arc::new(StaticRuntimeControl {
            ready: false,
            models: vec!["next".into()],
        }),
    ));
    assert!(!generation.ready());
    assert_eq!(generation.models(), vec!["next"]);
    assert_eq!(generation.active_version(), Some(3));
}

#[tokio::test]
async fn health_is_live_before_the_first_runtime_generation_is_ready() {
    let app = router(
        Arc::new(RuntimeGeneration::new()),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let health = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let readiness = app
        .oneshot(
            Request::builder()
                .uri("/readyz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(health.status(), StatusCode::OK);
    assert_eq!(readiness.status(), StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn metrics_endpoint_uses_vllm_openmetrics_registry() {
    let app = router(
        Arc::new(RecordingGeneration::default()),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let models = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/models")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(models.status(), StatusCode::OK);

    let status = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/statusz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(status.status(), StatusCode::OK);
    let status = axum::body::to_bytes(status.into_body(), usize::MAX)
        .await
        .unwrap();
    let status: serde_json::Value = serde_json::from_slice(&status).unwrap();
    assert_eq!(status["serving_ready"], true);
    assert_eq!(status["kv_index"]["state"], "degraded");
    assert_eq!(status["kv_index"]["reason"], "event_subscriber_unavailable");

    let response = app
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.headers()["content-type"],
        "application/openmetrics-text; version=1.0.0; charset=utf-8"
    );
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body = String::from_utf8(body.to_vec()).unwrap();
    assert!(body.contains("http_requests"));
    assert!(
        body.contains("foretoken_kv_index_degraded{reason=\"event_subscriber_unavailable\"} 1")
    );
}

#[tokio::test]
async fn text_and_chat_routes_fix_their_capabilities_and_chat_uses_messages() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(|| vec!["first".into()]),
        std::time::Duration::from_secs(1),
    );
    for (uri, body) in [
        (
            "/v1/completions",
            r#"{"model":"m","prompt":"text","request_id":"reused-by-client"}"#,
        ),
        (
            "/v1/chat/completions",
            r#"{"model":"m","messages":[{"role":"user","content":"chat"}],"request_id":"reused-by-client"}"#,
        ),
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(uri)
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    }
    let calls = generation.calls.lock().unwrap();
    assert_eq!(calls[0].0, "text");
    assert_eq!(calls[0].1, "text");
    assert_eq!(calls[1], ("chat".into(), "chat".into(), "chat".into()));
    drop(calls);
    let request_ids = generation.request_ids.lock().unwrap();
    assert!(request_ids.iter().all(|id| id != "reused-by-client"));
    assert!(request_ids.iter().any(|id| id.starts_with("cmpl-")));
    assert!(request_ids.iter().any(|id| id.starts_with("chatcmpl-")));
}

#[tokio::test]
async fn completion_preserves_token_prompts_and_supported_sampling_fields() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let body = r#"{"model":"m","prompt":[1,2,3],"stream":true,"temperature":0.4,"top_p":0.8,"top_k":20,"min_p":0.1,"seed":7,"max_tokens":64,"min_tokens":2,"frequency_penalty":0.2,"presence_penalty":0.3,"repetition_penalty":1.1,"stop_token_ids":[9],"ignore_eos":true,"logit_bias":{"10":-2.0},"allowed_token_ids":[10,11],"bad_words":["bad"],"logprobs":5,"prompt_logprobs":3,"priority":4,"cache_salt":"tenant"}"#;
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/completions")
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    let captured = generation.text_requests.lock().unwrap();
    let (prompt, sampling, intermediate, priority, cache_salt, arrival_time) = &captured[0];
    assert_eq!(prompt, &Prompt::TokenIds(vec![1, 2, 3]));
    assert_eq!(sampling.top_k, Some(20));
    assert_eq!(sampling.min_p, Some(0.1));
    assert_eq!(sampling.seed, Some(7));
    assert_eq!(sampling.min_tokens, Some(2));
    assert_eq!(sampling.stop_token_ids, Some(vec![9]));
    assert!(sampling.ignore_eos);
    assert_eq!(sampling.logprobs, Some(5));
    assert_eq!(sampling.prompt_logprobs, Some(3));
    assert!(*intermediate);
    assert_eq!(*priority, 4);
    assert_eq!(cache_salt.as_deref(), Some("tenant"));
    assert!(arrival_time.is_some());
}

#[tokio::test]
async fn completion_accepts_prompt_forms_and_rejects_unbounded_fanout() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/completions")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"model":"m","prompt":["first","second"],"n":2,"best_of":3}"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    {
        let captured = generation.text_requests.lock().unwrap();
        // The recording backend rejects the first candidate, but the request has already been
        // lowered as the first member of the bounded multi-prompt/best-of fan-out.
        assert_eq!(captured.len(), 1);
        assert!(matches!(captured[0].0, Prompt::Text(ref text) if text == "first"));
        assert_eq!(captured[0].1.logprobs, Some(0));
    }

    for prompt in [r#"[1,2,3]"#, r#"[[4,5],[6]]"#] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(format!(r#"{{"model":"m","prompt":{prompt}}}"#)))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    }
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/completions")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"model":"m","prompt":"x","n":17}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn completion_rejects_streaming_multi_prompt_best_of_and_transfer_injection() {
    let app = router(
        Arc::new(RecordingGeneration::default()),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    for body in [
        r#"{"model":"m","prompt":["a","b"],"stream":true}"#,
        r#"{"model":"m","prompt":"a","stream":true,"best_of":2}"#,
        r#"{"model":"m","prompt":"a","kv_transfer_params":{}}"#,
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
}

#[tokio::test]
async fn generated_request_ids_are_unique_and_models_are_dynamic() {
    let generation = Arc::new(RecordingGeneration::default());
    let models = Arc::new(Mutex::new(vec!["first".to_owned()]));
    let source = models.clone();
    let app = router(
        generation.clone(),
        Arc::new(move || source.lock().unwrap().clone()),
        std::time::Duration::from_secs(1),
    );
    for _ in 0..2 {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"model":"m","prompt":"p"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    }
    {
        let calls = generation.calls.lock().unwrap();
        assert_ne!(calls[0].2, calls[1].2);
    }
    *models.lock().unwrap() = vec!["second".to_owned()];
    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/models")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert!(std::str::from_utf8(&body).unwrap().contains("second"));
}

#[tokio::test]
async fn concurrent_reused_client_request_ids_get_distinct_backend_ids() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let requests = (0..2).map(|_| {
        app.clone().oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/completions")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"model":"m","prompt":"p","request_id":"reused-by-client"}"#,
                ))
                .unwrap(),
        )
    });

    let responses = futures::future::join_all(requests).await;
    assert!(
        responses
            .iter()
            .all(|response| response.as_ref().unwrap().status() == StatusCode::BAD_GATEWAY)
    );
    let request_ids = generation.request_ids.lock().unwrap();
    assert_eq!(request_ids.len(), 2);
    assert!(request_ids.iter().all(|id| id.starts_with("cmpl-")));
    assert!(request_ids.iter().all(|id| id != "reused-by-client"));
    assert_ne!(request_ids[0], request_ids[1]);
}

#[tokio::test]
async fn stream_and_nonstream_share_vllm_incremental_decoding() {
    use foretoken_text::TextOutputStreamExt as _;
    use foretoken_text::output::decoded_text_event_stream;
    use foretoken_tokenizer::{Result as TokenizerResult, Tokenizer};
    use futures::StreamExt as _;
    use vllm_llm::{FinishReason, GenerateOutput};

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
            char::from_u32(id).map(|c| c.to_string())
        }
    }
    let raw = || {
        futures::stream::iter([Ok(GenerateOutput {
            request_id: "request".into(),
            prompt_info: Some(vllm_llm::GeneratePromptInfo {
                prompt_token_ids: vec![1].into(),
                prompt_logprobs: None,
            }),
            token_ids: vec![b'h' as u32, b'i' as u32],
            logprobs: None,
            finish_reason: Some(FinishReason::Length),
            cached_token_count: 0,
            kv_transfer_params: None,
            ec_transfer_params: None,
        })])
    };
    let tokenizer = Arc::new(ByteTokenizer);
    let collected = decoded_text_event_stream(
        "request".into(),
        tokenizer.clone(),
        raw(),
        Default::default(),
        true,
    )
    .collect_output()
    .await
    .unwrap();
    let deltas =
        decoded_text_event_stream("request".into(), tokenizer, raw(), Default::default(), true)
            .filter_map(|event| async move {
                match event.unwrap() {
                    foretoken_text::DecodedTextEvent::TextDelta { delta, .. } => Some(delta),
                    _ => None,
                }
            })
            .collect::<Vec<_>>()
            .await;
    assert_eq!(collected.text, deltas.concat());
    assert_eq!(collected.text, "hi");
}

#[tokio::test]
async fn completion_responses_share_typed_metadata_and_separate_the_terminal_chunk() {
    use vllm_llm::FinishReason;

    let response = text_stream(
        generated_text("cmpl-contract", "served-model", FinishReason::Length),
        std::time::Duration::from_secs(1),
    );
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body = std::str::from_utf8(&body).unwrap();
    let chunks = body
        .lines()
        .filter_map(|line| line.strip_prefix("data: "))
        .filter(|data| *data != "[DONE]")
        .map(|data| serde_json::from_str::<serde_json::Value>(data).unwrap())
        .collect::<Vec<_>>();

    assert_eq!(chunks.len(), 2);
    for chunk in &chunks {
        assert_eq!(chunk["id"], "cmpl-contract");
        assert_eq!(chunk["model"], "served-model");
        assert_eq!(chunk["object"], "text_completion");
        assert!(chunk["created"].as_u64().unwrap() > 0);
    }
    assert_eq!(chunks[0]["choices"][0]["text"], "hi");
    assert!(chunks[0]["choices"][0].get("finish_reason").is_none());
    assert_eq!(chunks[1]["choices"][0]["text"], "");
    assert_eq!(chunks[1]["choices"][0]["finish_reason"], "length");

    let response = text_collected(
        generated_text("cmpl-collected", "served-model", FinishReason::Length),
        std::time::Duration::from_secs(1),
    )
    .await;
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["id"], "cmpl-collected");
    assert_eq!(body["model"], "served-model");
    assert!(body["created"].as_u64().unwrap() > 0);
}

#[tokio::test]
async fn completion_raw_ids_require_explicit_opt_in() {
    let response = text_collected_many(
        vec![generated_text(
            "cmpl-ids",
            "served-model",
            vllm_llm::FinishReason::Length,
        )],
        std::time::Duration::from_secs(1),
        CompletionResponseOptions {
            n: 1,
            candidates_per_prompt: 1,
            echo: true,
            expose_logprobs: false,
            return_token_ids: true,
            return_prompt_token_ids: true,
        },
    )
    .await;
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        body["choices"][0]["token_ids"],
        serde_json::json!([104, 105])
    );
    assert_eq!(body["prompt_token_ids"], serde_json::json!([[1]]));
    assert!(body["choices"][0]["text"].as_str().unwrap().ends_with("hi"));
    assert!(body["choices"][0].get("logprobs").is_none());
}

#[tokio::test]
async fn completion_logprobs_usage_and_stop_reason_follow_openai_schema() {
    use vllm_engine_core_client::protocol::output::StopReason;
    use vllm_llm::FinishReason;

    let response = text_stream_with_options(
        generated_text_with_logprobs(
            "cmpl-logprobs-stream",
            "served-model",
            FinishReason::Stop(Some(StopReason::Text("halt".into()))),
        ),
        std::time::Duration::from_secs(1),
        true,
    );
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let chunks = std::str::from_utf8(&body)
        .unwrap()
        .lines()
        .filter_map(|line| line.strip_prefix("data: "))
        .filter(|data| *data != "[DONE]")
        .map(|data| serde_json::from_str::<serde_json::Value>(data).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(
        chunks[0]["choices"][0]["logprobs"]["tokens"],
        serde_json::json!(["h", "i"])
    );
    assert_eq!(
        chunks[0]["choices"][0]["logprobs"]["token_logprobs"],
        serde_json::json!([-0.1, -0.2])
    );
    assert!(chunks[0]["choices"][0].get("token_ids").is_none());
    assert_eq!(chunks[1]["choices"][0]["stop_reason"], "halt");
    assert_eq!(chunks[2]["choices"], serde_json::json!([]));
    assert_eq!(
        chunks[2]["usage"]["prompt_tokens_details"]["cached_tokens"],
        1
    );

    let response = text_collected(
        generated_text_with_logprobs("cmpl-logprobs", "served-model", FinishReason::Length),
        std::time::Duration::from_secs(1),
    )
    .await;
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        body["choices"][0]["logprobs"]["tokens"],
        serde_json::json!(["h", "i"])
    );
    assert_eq!(body["usage"]["prompt_tokens_details"]["cached_tokens"], 1);
    assert!(body["choices"][0].get("token_ids").is_none());
    assert!(body.get("prompt_token_ids").is_none());
}

#[tokio::test]
async fn error_finish_reason_is_an_openai_error_not_a_successful_finish() {
    use vllm_llm::FinishReason;

    let response = text_collected(
        generated_text("cmpl-error", "served-model", FinishReason::Error),
        std::time::Duration::from_secs(1),
    )
    .await;
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["error"]["code"], "request_failed");

    let response = text_stream(
        generated_text("cmpl-error", "served-model", FinishReason::Error),
        std::time::Duration::from_secs(1),
    );
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body = std::str::from_utf8(&body).unwrap();
    assert!(body.contains(r#""error":{"message":"generation backend request failed"#));
    assert!(!body.contains(r#""finish_reason":"error"#));
    assert_eq!(body.matches("[DONE]").count(), 1);
}

#[tokio::test]
async fn chat_response_uses_vllms_structured_output_processor() {
    use foretoken_router::{BackendId, RouteDecision};
    use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
    use vllm_llm::{
        FinishReason, GenerateOutput, GeneratePromptInfo, GenerateRequest, Logprobs,
        PositionLogprobs, TokenLogprob,
    };

    let tokenizer: DynTokenizer = Arc::new(TestTokenizer);
    let mut chat = ChatRequest {
        request_id: "chatcmpl-test".into(),
        messages: vec![ChatMessage::User {
            content: "hello".into(),
        }],
        sampling_params: Default::default(),
        chat_options: Default::default(),
        tools: Vec::new(),
        tool_choice: foretoken_chat::ChatToolChoice::None,
        parallel_tool_calls: false,
        decode_options: Default::default(),
        intermediate: true,
        priority: 0,
        documents: None,
        cache_salt: None,
        add_special_tokens: false,
        data_parallel_rank: None,
        lora_request: None,
    };
    let tool_call_parser = foretoken_chat::ParserSelection::Auto;
    let reasoning_parser = foretoken_chat::ParserSelection::Auto;
    let output_processor: DynChatOutputProcessor = Box::new(
        DefaultChatOutputProcessor::new(
            &mut chat,
            "test",
            tokenizer.clone(),
            &tool_call_parser,
            &reasoning_parser,
        )
        .unwrap(),
    );
    let request = GenerateRequest {
        request_id: chat.request_id.clone(),
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
    };
    let generated = GeneratedChat {
        generated: Generated {
            routed: RoutedGenerate {
                routed_request: RoutedRequest {
                    decision: RouteDecision {
                        backend_id: BackendId::new("group-a"),
                        model: "test".into(),
                        revision: "r1".into(),
                    },
                    request,
                },
                stream: token_stream(vec![
                    Ok(GenerateOutput {
                        request_id: chat.request_id.clone(),
                        prompt_info: Some(GeneratePromptInfo {
                            prompt_token_ids: vec![1].into(),
                            prompt_logprobs: None,
                        }),
                        token_ids: vec![b'h' as u32],
                        logprobs: Some(Logprobs {
                            positions: vec![PositionLogprobs {
                                entries: vec![TokenLogprob {
                                    token_id: b'h' as u32,
                                    logprob: -0.1,
                                    rank: 1,
                                }],
                            }],
                        }),
                        finish_reason: None,
                        cached_token_count: 0,
                        kv_transfer_params: None,
                        ec_transfer_params: None,
                    }),
                    Ok(GenerateOutput {
                        request_id: chat.request_id,
                        prompt_info: None,
                        token_ids: vec![b'i' as u32],
                        logprobs: Some(Logprobs {
                            positions: vec![PositionLogprobs {
                                entries: vec![TokenLogprob {
                                    token_id: b'i' as u32,
                                    logprob: -0.2,
                                    rank: 1,
                                }],
                            }],
                        }),
                        finish_reason: Some(FinishReason::stop_eos()),
                        cached_token_count: 0,
                        kv_transfer_params: None,
                        ec_transfer_params: None,
                    }),
                ]),
            },
            tokenizer,
            decode_options: chat.decode_options,
        },
        output_processor,
        include_reasoning: true,
    };

    let response = chat_collected(generated, std::time::Duration::from_secs(1)).await;
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body = std::str::from_utf8(&body).unwrap();

    assert!(body.contains(r#""id":"chatcmpl-test""#));
    assert!(body.contains(r#""model":"test""#));
    assert!(body.contains(r#""created":"#));
    assert!(body.contains(r#""reasoning":null"#));
    assert!(!body.contains(r#""tool_calls":[]"#));
    assert!(body.contains(r#""prompt_tokens":1"#));
    assert!(body.contains(r#""completion_tokens":2"#));
    assert!(body.contains(r#""logprobs":{"content":[{"token":"h","logprob":-0.1"#));
    assert!(!body.contains(r#""token_ids"#));
}

#[tokio::test]
async fn stream_idle_timeout_terminates_a_silent_output_stream() {
    use futures::StreamExt as _;

    let silent =
        futures::stream::pending::<foretoken_text::Result<foretoken_text::DecodedTextEvent>>();
    let mut timed = Box::pin(idle_timed(silent, std::time::Duration::from_millis(1)));

    assert!(timed.next().await.unwrap().is_err());
    assert!(timed.next().await.is_none());
}

#[tokio::test]
async fn backend_stream_error_emits_one_done_after_the_error() {
    let response = stream_response(stream::iter([Err::<foretoken_text::DecodedTextEvent, _>(
        foretoken_text::Error::StreamClosedBeforeTerminalOutput {
            request_id: "request".into(),
        },
    )]));
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body = std::str::from_utf8(&body).unwrap();
    assert!(body.contains("generation backend request failed"));
    assert_eq!(body.matches("[DONE]").count(), 1);
}

#[tokio::test]
async fn unsupported_chat_features_and_prompt_dto_on_chat_are_client_errors() {
    let app = router(
        Arc::new(RecordingGeneration::default()),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    for body in [
        r#"{"model":"m","prompt":"wrong"}"#,
        r#"{"model":"m","messages":[{"role":"user","content":"x"}],"chat_template":"forbidden"}"#,
        r#"{"model":"m","messages":[{"role":"user","content":"x"}],"unsupported":true}"#,
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/chat/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert!(response.status().is_client_error());
        let body = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(body["error"]["type"], "invalid_request_error");
        assert_eq!(body["error"]["code"], "invalid_request");
    }
}

#[tokio::test]
async fn chat_maps_tool_history_choice_reasoning_and_structured_output() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let response = app.oneshot(Request::builder().method("POST").uri("/v1/chat/completions").header("content-type", "application/json").body(Body::from(r#"{"model":"m","include_reasoning":false,"parallel_tool_calls":true,"tools":[{"type":"function","function":{"name":"weather","parameters":{"type":"object"}}}],"tool_choice":{"type":"function","function":{"name":"weather"}},"response_format":{"type":"json_object"},"messages":[{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"weather","arguments":"{}"}}]},{"role":"tool","tool_call_id":"call_1","content":"sunny"},{"role":"user","content":"continue"}]}"#)).unwrap()).await.unwrap();
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    let captured = generation.chat_requests.lock().unwrap();
    let (capabilities, chat, include_reasoning) = &captured[0];
    assert!(!include_reasoning);
    assert!(capabilities.is_superset(&BTreeSet::from([
        "chat".into(),
        "tool_calling".into(),
        "structured_output.json_object".into()
    ])));
    assert_eq!(chat.tools[0].name, "weather");
    assert_eq!(
        chat.tool_choice,
        foretoken_chat::ChatToolChoice::Function {
            name: "weather".into()
        }
    );
    assert!(chat.parallel_tool_calls);
    assert!(matches!(chat.messages[0], ChatMessage::Assistant { .. }));
    assert!(
        matches!(chat.messages[1], ChatMessage::ToolResponse { ref tool_call_id, .. } if tool_call_id == "call_1")
    );
    assert!(chat.sampling_params.structured_outputs.is_some());
}

#[tokio::test]
async fn chat_accepts_openai_multimodal_parts_and_gates_each_modality() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let body = r#"{"model":"m","messages":[{"role":"system","content":"inspect media"},{"role":"user","content":[{"type":"text","text":"describe"},{"type":"image_url","image_url":{"url":"data:image/png;base64,AA==","detail":"low"}},{"type":"video_url","video_url":{"url":"data:video/mp4;base64,AA=="}},{"type":"input_audio","input_audio":{"data":"AA==","format":"wav"}}]}]}"#;
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/chat/completions")
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    let captured = generation.chat_requests.lock().unwrap();
    let (capabilities, chat, _) = &captured[0];
    assert!(capabilities.is_superset(&BTreeSet::from([
        "chat".into(),
        "multimodal".into(),
        "multimodal.image".into(),
        "multimodal.video".into(),
        "multimodal.audio".into(),
    ])));
    assert!(matches!(
        chat.messages[1],
        ChatMessage::User { ref content } if content.has_multimodal()
    ));
    assert!(!chat.intermediate);
}

#[tokio::test]
async fn chat_reasoning_and_json_schema_are_capability_gated_and_invalid_extensions_reject() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let valid = r#"{"model":"m","reasoning_effort":"high","include_reasoning":true,"response_format":{"type":"json_schema","json_schema":{"name":"answer","schema":{"type":"object"}}},"messages":[{"role":"user","content":"x"}]}"#;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/chat/completions")
                .header("content-type", "application/json")
                .body(Body::from(valid))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    {
        let captured = generation.chat_requests.lock().unwrap();
        assert!(captured[0].0.is_superset(&BTreeSet::from([
            "chat".into(),
            "reasoning".into(),
            "structured_output.json_schema".into()
        ])));
    }
    for invalid in [
        r#"{"model":"m","messages":[{"role":"user","content":"x"}],"kv_transfer_params":{}}"#,
        r#"{"model":"m","messages":[{"role":"user","content":"x"}],"response_format":{"type":"json_schema","json_schema":{"name":"answer","schema":"not-an-object"}}}"#,
        r#"{"model":"m","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://169.254.169.254/latest/meta-data"}}]}]}"#,
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/chat/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(invalid))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
}

#[tokio::test]
async fn model_and_tokenizer_endpoints_follow_vllm_shapes() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation,
        Arc::new(|| vec!["m".into()]),
        std::time::Duration::from_secs(1),
    );

    let model = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/models/m")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(model.status(), StatusCode::OK);

    let tokenized = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/tokenize")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"model":"m","prompt":"ab","return_token_strs":true}"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(tokenized.status(), StatusCode::OK);
    let body = axum::body::to_bytes(tokenized.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["count"], 2);
    assert_eq!(body["max_model_len"], 4096);
    assert_eq!(body["tokens"], serde_json::json!([97, 98]));

    let detokenized = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/detokenize")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"model":"m","tokens":[97,98]}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(detokenized.status(), StatusCode::OK);
    let body = axum::body::to_bytes(detokenized.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&body).unwrap()["prompt"],
        "ab"
    );
}

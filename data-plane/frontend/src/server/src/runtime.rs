// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Owns immutable model runtimes and request dispatch.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use arc_swap::ArcSwapOption;
use async_trait::async_trait;
use foretoken_chat::{
    ChatRequest, ChatRequestProcessor, DynChatOutputProcessor, NewChatOutputProcessorOptions,
    ParserSelection,
};
use foretoken_llm_facade::{LlmFacadeError, LlmFacadeResolver, TokenStream};
use foretoken_router::{RouteDecision, RouteTargetSet, Router, RouterRequest};
use foretoken_text::{
    Prompt, SamplingParams, TextDecodeOptions, TextRequest, TextRequestProcessor,
};
use foretoken_tokenizer::DynTokenizer;
use serde::Serialize;
use thiserror::Error;

pub(crate) mod workflow;

use workflow::execute_workflow;

/// The tokenizer and chat renderer from one immutable model snapshot.
pub struct RuntimeBundle {
    pub text_processor: Arc<TextRequestProcessor>,
    pub tokenizer: DynTokenizer,
    pub chat_processor: Arc<ChatRequestProcessor>,
}

impl RuntimeBundle {
    pub fn new(
        text_processor: Arc<TextRequestProcessor>,
        tokenizer: DynTokenizer,
        chat_processor: Arc<ChatRequestProcessor>,
    ) -> Self {
        Self {
            text_processor,
            tokenizer,
            chat_processor,
        }
    }
}

/// Input after HTTP has derived the request's routing and output-processing intent.
pub struct GenerationRequest {
    pub model: String,
    pub request_id: String,
    pub prompt: Prompt,
    pub sampling_params: SamplingParams,
    pub decode_options: TextDecodeOptions,
    pub intermediate: bool,
    pub priority: i32,
    pub cache_salt: Option<String>,
    pub session_id: Option<String>,
    pub arrival_time: Option<f64>,
    pub tool_call_parser: ParserSelection,
    pub reasoning_parser: ParserSelection,
}

pub struct RoutedRequest {
    pub decision: RouteDecision,
    pub request: vllm_llm::GenerateRequest,
}

pub struct RoutedGenerate {
    pub routed_request: RoutedRequest,
    pub stream: TokenStream,
}

pub struct Generated {
    pub routed: RoutedGenerate,
    pub tokenizer: DynTokenizer,
    pub decode_options: TextDecodeOptions,
}

pub struct GeneratedChat {
    pub generated: Generated,
    pub output_processor: DynChatOutputProcessor,
    pub include_reasoning: bool,
}

pub struct Tokenization {
    pub token_ids: Vec<u32>,
    pub token_strs: Option<Vec<String>>,
    pub max_model_len: u32,
}

/// Stable failure categories for HTTP generation before a stream begins.
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum GenerationError {
    #[error("request is invalid")]
    InvalidRequest,
    #[error("model was not found")]
    ModelNotFound,
    #[error("no backend is available")]
    Unavailable,
    #[error("backend rejected request")]
    BackendRejected,
    #[error("backend protocol failed")]
    BackendProtocol,
    #[error("backend request failed")]
    RequestFailed,
    #[error("frontend internal error")]
    Internal,
}

impl From<LlmFacadeError> for GenerationError {
    fn from(error: LlmFacadeError) -> Self {
        match error {
            LlmFacadeError::Unavailable => Self::Unavailable,
            LlmFacadeError::Rejected => Self::BackendRejected,
            LlmFacadeError::Protocol => Self::BackendProtocol,
            LlmFacadeError::RequestFailed => Self::RequestFailed,
            LlmFacadeError::Configuration => Self::Internal,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct KvIndexDiagnostics {
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    pub sources_healthy: usize,
    pub sources_total: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RuntimeDiagnostics {
    pub serving_ready: bool,
    pub active_generation: Option<u64>,
    pub kv_index: KvIndexDiagnostics,
}

#[async_trait]
pub trait RuntimeControl: Send + Sync {
    async fn refresh_backend_readiness(&self);
    fn configured_models(&self) -> Vec<String>;
    fn is_ready(&self) -> bool;
    #[allow(unused_variables)]
    fn model_ready(&self, model: &str) -> bool {
        self.is_ready()
    }
    fn kv_index_diagnostics(&self) -> KvIndexDiagnostics {
        KvIndexDiagnostics {
            state: "unavailable".into(),
            reason: Some("not_reported".into()),
            sources_healthy: 0,
            sources_total: 0,
        }
    }
}

#[async_trait]
pub trait Generation: Send + Sync {
    async fn generate(&self, request: GenerationRequest) -> Result<Generated, GenerationError>;
    async fn generate_chat(
        &self,
        request: GenerationRequest,
        chat: ChatRequest,
        include_reasoning: bool,
    ) -> Result<GeneratedChat, GenerationError>;
    async fn tokenize(
        &self,
        _model: &str,
        _prompt: Prompt,
        _add_special_tokens: bool,
        _return_token_strs: bool,
    ) -> Result<Tokenization, GenerationError> {
        Err(GenerationError::Internal)
    }
    async fn tokenize_chat(
        &self,
        _model: &str,
        _chat: ChatRequest,
        _return_token_strs: bool,
    ) -> Result<Tokenization, GenerationError> {
        Err(GenerationError::Internal)
    }
    async fn detokenize(
        &self,
        _model: &str,
        _token_ids: &[u32],
    ) -> Result<String, GenerationError> {
        Err(GenerationError::Internal)
    }
    fn ready(&self) -> bool;
    fn diagnostics(&self) -> RuntimeDiagnostics {
        RuntimeDiagnostics {
            serving_ready: self.ready(),
            active_generation: None,
            kv_index: KvIndexDiagnostics {
                state: "unavailable".into(),
                reason: Some("no_active_generation".into()),
                sources_healthy: 0,
                sources_total: 0,
            },
        }
    }
}

/// Request-processing artifacts for one public model identity.
pub struct ModelRuntime {
    bundle: Arc<RuntimeBundle>,
}

impl ModelRuntime {
    pub fn new(bundle: Arc<RuntimeBundle>) -> Self {
        Self { bundle }
    }
}

struct AdmissionTargets {
    candidates: Vec<RouteTargetSet>,
    next: AtomicU64,
}

impl AdmissionTargets {
    fn select(&self) -> RouteTargetSet {
        let index = self.next.fetch_add(1, Ordering::Relaxed) as usize % self.candidates.len();
        self.candidates[index].clone()
    }
}

/// All request-processing objects derived from one routing snapshot.
///
/// The Router and LLM facade resolver are shared across models, while every model owns its
/// immutable vLLM request processors.
pub struct RuntimeState {
    models: BTreeMap<String, ModelRuntime>,
    admission_targets: BTreeMap<String, AdmissionTargets>,
    router: Arc<dyn Router>,
    resolver: Arc<dyn LlmFacadeResolver>,
}

impl RuntimeState {
    pub fn new(
        models: BTreeMap<String, ModelRuntime>,
        router: Arc<dyn Router>,
        resolver: Arc<dyn LlmFacadeResolver>,
    ) -> Self {
        Self {
            models,
            admission_targets: BTreeMap::new(),
            router,
            resolver,
        }
    }

    pub fn with_admission_targets(
        mut self,
        model: String,
        candidates: Vec<RouteTargetSet>,
    ) -> Self {
        if !candidates.is_empty() {
            self.admission_targets.insert(
                model,
                AdmissionTargets {
                    candidates,
                    next: AtomicU64::new(0),
                },
            );
        }
        self
    }

    fn select_admission_targets(&self, model: &str) -> Option<RouteTargetSet> {
        self.admission_targets
            .get(model)
            .map(AdmissionTargets::select)
    }

    pub fn model(&self, model: &str) -> Result<&ModelRuntime, GenerationError> {
        self.models.get(model).ok_or(GenerationError::ModelNotFound)
    }
}

/// A complete published generation runtime.
///
/// State and control originate from the same routing snapshot and must therefore be read as one
/// atomic slot.
struct RuntimeSlot {
    version: u64,
    state: Arc<RuntimeState>,
    control: Arc<dyn RuntimeControl>,
}

/// Real adapter: each request loads one immutable runtime state before lowering and routing.
pub struct RuntimeGeneration {
    slot: ArcSwapOption<RuntimeSlot>,
    publication: Mutex<()>,
    publication_updates: tokio::sync::watch::Sender<u64>,
    accepting: AtomicBool,
    request_timeout: Duration,
}

impl RuntimeGeneration {
    pub fn new(request_timeout: Duration) -> Self {
        let (publication_updates, _) = tokio::sync::watch::channel(0);
        Self {
            slot: ArcSwapOption::empty(),
            publication: Mutex::new(()),
            publication_updates,
            accepting: AtomicBool::new(true),
            request_timeout,
        }
    }

    /// Publishes a newer serving generation without allowing concurrent stale writers to win.
    pub fn replace_state(
        &self,
        version: u64,
        state: Arc<RuntimeState>,
        control: Arc<dyn RuntimeControl>,
    ) -> bool {
        let _publication = self
            .publication
            .lock()
            .expect("runtime publication lock poisoned");
        if self
            .slot
            .load_full()
            .is_some_and(|active| active.version >= version)
        {
            return false;
        }
        self.slot.store(Some(Arc::new(RuntimeSlot {
            version,
            state,
            control,
        })));
        self.publication_updates.send_replace(version);
        true
    }

    pub fn close_admission(&self) {
        self.accepting.store(false, Ordering::Release);
        self.publication_updates
            .send_replace(self.slot.load_full().map_or(0, |slot| slot.version));
    }

    pub fn active_version(&self) -> Option<u64> {
        self.slot.load_full().map(|slot| slot.version)
    }

    pub async fn refresh_backend_readiness(&self) {
        if let Some(slot) = self.slot.load_full() {
            slot.control.refresh_backend_readiness().await;
            self.publication_updates.send_replace(slot.version);
        }
    }

    pub fn configured_models(&self) -> Vec<String> {
        self.slot
            .load_full()
            .map(|slot| slot.control.configured_models())
            .unwrap_or_default()
    }

    fn ready_state(&self) -> Result<Arc<RuntimeSlot>, GenerationError> {
        if !self.accepting.load(Ordering::Acquire) {
            return Err(GenerationError::Unavailable);
        }
        let slot = self.slot.load_full().ok_or(GenerationError::Unavailable)?;
        if slot.control.is_ready() {
            Ok(slot)
        } else {
            Err(GenerationError::Unavailable)
        }
    }

    async fn generation_slot(&self, model: &str) -> Result<Arc<RuntimeSlot>, GenerationError> {
        // A configured model without a prepared processor is admission-only: keep its targets
        // queued while waiting, then reload the complete slot across each generation boundary.
        let deadline = Instant::now() + self.request_timeout;
        let mut publication_updates = self.publication_updates.subscribe();
        let mut queued = None;
        loop {
            let slot = self.ready_state()?;
            if slot.state.model(model).is_ok() && slot.control.model_ready(model) {
                return Ok(slot);
            }
            let Some(targets) = slot.state.select_admission_targets(model) else {
                return Err(GenerationError::ModelNotFound);
            };
            if queued.is_none() {
                foretoken_metrics::register_targets(&targets);
                queued = Some(foretoken_metrics::QueueGuard::new(&targets));
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero()
                || !matches!(
                    tokio::time::timeout(remaining, publication_updates.changed()).await,
                    Ok(Ok(()))
                )
                || !self.accepting.load(Ordering::Acquire)
            {
                return Err(GenerationError::Unavailable);
            }
        }
    }

    async fn dispatch(
        &self,
        slot: Arc<RuntimeSlot>,
        runtime: &ModelRuntime,
        request: GenerationRequest,
        text_request: TextRequest,
    ) -> Result<Generated, GenerationError> {
        let bundle = &runtime.bundle;
        let decode_options = text_request.decode_options.clone();
        let prepared = bundle
            .text_processor
            .prepare(text_request)
            .map_err(|error| {
                if error.is_request_validation_error() {
                    GenerationError::InvalidRequest
                } else {
                    GenerationError::Internal
                }
            })?;
        let generate_request = prepared.generate_request;
        let context = RouterRequest::new(request.model.clone(), Arc::new(generate_request.clone()));
        let mut session = slot.state.router.start(context);
        let initial = session
            .select_initial()
            .map_err(|_| GenerationError::Unavailable)?;
        foretoken_metrics::register_targets(&initial.admission_targets);
        let _queue = foretoken_metrics::QueueGuard::new(&initial.admission_targets);
        let (decision, backend_stream) = execute_workflow(
            &*slot.state.resolver,
            &mut *session,
            initial,
            generate_request.clone(),
        )
        .await?;
        let stream = backend_stream;
        let routed = RoutedGenerate {
            routed_request: RoutedRequest {
                decision,
                request: generate_request,
            },
            stream,
        };
        Ok(Generated {
            routed,
            tokenizer: bundle.tokenizer.clone(),
            decode_options,
        })
    }
}

#[async_trait]
impl Generation for RuntimeGeneration {
    async fn generate(&self, request: GenerationRequest) -> Result<Generated, GenerationError> {
        let slot = self.generation_slot(&request.model).await?;
        let runtime = slot.state.model(&request.model)?;
        let text_request = TextRequest {
            request_id: request.request_id.clone(),
            prompt: request.prompt.clone(),
            mm_features: None,
            sampling_params: request.sampling_params.clone(),
            decode_options: request.decode_options.clone(),
            intermediate: request.intermediate,
            priority: request.priority,
            cache_salt: request.cache_salt.clone(),
            add_special_tokens: false,
            data_parallel_rank: None,
            session_id: request.session_id.clone(),
            reasoning_parser_kwargs: None,
            lora_request: None,
            arrival_time: request.arrival_time,
        };
        self.dispatch(slot.clone(), runtime, request, text_request)
            .await
    }

    async fn generate_chat(
        &self,
        request: GenerationRequest,
        chat: ChatRequest,
        include_reasoning: bool,
    ) -> Result<GeneratedChat, GenerationError> {
        let slot = self.generation_slot(&request.model).await?;
        let runtime = slot.state.model(&request.model)?;
        let (text_request, output_processor) = runtime
            .bundle
            .chat_processor
            .prepare_with_options(
                chat,
                NewChatOutputProcessorOptions {
                    tool_call_parser: &request.tool_call_parser,
                    reasoning_parser: &request.reasoning_parser,
                },
            )
            .await
            .map_err(|error| {
                if error.is_request_validation_error() {
                    GenerationError::InvalidRequest
                } else {
                    GenerationError::Internal
                }
            })?;
        let generated = self
            .dispatch(slot.clone(), runtime, request, text_request)
            .await?;
        Ok(GeneratedChat {
            generated,
            output_processor,
            include_reasoning,
        })
    }

    async fn tokenize(
        &self,
        model: &str,
        prompt: Prompt,
        add_special_tokens: bool,
        return_token_strs: bool,
    ) -> Result<Tokenization, GenerationError> {
        let slot = self.ready_state()?;
        let runtime = slot.state.model(model)?;
        let tokenizer = &runtime.bundle.tokenizer;
        let token_ids = match prompt {
            Prompt::Text(text) => tokenizer
                .encode(&text, add_special_tokens)
                .map_err(|_| GenerationError::InvalidRequest)?,
            Prompt::TokenIds(token_ids) => token_ids,
        };
        let token_strs = if return_token_strs {
            Some(
                token_ids
                    .iter()
                    .map(|token_id| tokenizer.id_to_token(*token_id))
                    .collect::<Option<Vec<_>>>()
                    .ok_or(GenerationError::InvalidRequest)?,
            )
        } else {
            None
        };
        Ok(Tokenization {
            token_ids,
            token_strs,
            max_model_len: runtime.bundle.text_processor.max_model_len(),
        })
    }

    async fn tokenize_chat(
        &self,
        model: &str,
        chat: ChatRequest,
        return_token_strs: bool,
    ) -> Result<Tokenization, GenerationError> {
        let slot = self.ready_state()?;
        let runtime = slot.state.model(model)?;
        let text_request = runtime
            .bundle
            .chat_processor
            .prepare_for_tokenization(chat)
            .await
            .map_err(|_| GenerationError::InvalidRequest)?;
        let prepared = runtime
            .bundle
            .text_processor
            .prepare(text_request)
            .map_err(|_| GenerationError::InvalidRequest)?;
        let token_ids = prepared.generate_request.prompt_token_ids;
        let tokenizer = &runtime.bundle.tokenizer;
        let token_strs = if return_token_strs {
            Some(
                token_ids
                    .iter()
                    .map(|token_id| tokenizer.id_to_token(*token_id))
                    .collect::<Option<Vec<_>>>()
                    .ok_or(GenerationError::InvalidRequest)?,
            )
        } else {
            None
        };
        Ok(Tokenization {
            token_ids,
            token_strs,
            max_model_len: runtime.bundle.text_processor.max_model_len(),
        })
    }

    async fn detokenize(&self, model: &str, token_ids: &[u32]) -> Result<String, GenerationError> {
        let slot = self.ready_state()?;
        let runtime = slot.state.model(model)?;
        runtime
            .bundle
            .tokenizer
            .decode(token_ids, false)
            .map_err(|_| GenerationError::InvalidRequest)
    }

    fn ready(&self) -> bool {
        self.accepting.load(Ordering::Acquire)
            && self
                .slot
                .load_full()
                .is_some_and(|slot| slot.control.is_ready())
    }

    fn diagnostics(&self) -> RuntimeDiagnostics {
        let Some(slot) = self.slot.load_full() else {
            return RuntimeDiagnostics {
                serving_ready: false,
                active_generation: None,
                kv_index: KvIndexDiagnostics {
                    state: "unavailable".into(),
                    reason: Some("no_active_generation".into()),
                    sources_healthy: 0,
                    sources_total: 0,
                },
            };
        };
        RuntimeDiagnostics {
            serving_ready: self.accepting.load(Ordering::Acquire) && slot.control.is_ready(),
            active_generation: Some(slot.version),
            kv_index: slot.control.kv_index_diagnostics(),
        }
    }
}

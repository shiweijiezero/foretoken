// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Owns immutable model runtimes and request dispatch.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use arc_swap::ArcSwapOption;
use async_trait::async_trait;
use foretoken_chat::{
    ChatRequest, ChatRequestProcessor, DynChatOutputProcessor, NewChatOutputProcessorOptions,
    ParserSelection,
};
use foretoken_llm_facade::{LlmFacadeError, LlmFacadeResolver, TokenStream, abort_on_drop};
use foretoken_router::{RouteContext, RouteDecision, RouteSelection, Router};
use foretoken_text::{
    Prompt, SamplingParams, TextDecodeOptions, TextRequest, TextRequestProcessor,
};
use foretoken_tokenizer::DynTokenizer;
use futures::StreamExt;
use serde::Serialize;
use thiserror::Error;

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
    pub revision: Option<String>,
    pub required_capabilities: BTreeSet<String>,
    pub prompt: Prompt,
    pub sampling_params: SamplingParams,
    pub decode_options: TextDecodeOptions,
    pub intermediate: bool,
    pub priority: i32,
    pub cache_salt: Option<String>,
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
    fn healthy_models(&self) -> Vec<String>;
    fn is_ready(&self) -> bool;
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

/// The pinned request-processing artifacts for one public model identity.
pub struct ModelRuntime {
    revision: String,
    bundle: Arc<RuntimeBundle>,
}

impl ModelRuntime {
    pub fn new(revision: String, bundle: Arc<RuntimeBundle>) -> Self {
        Self { revision, bundle }
    }

    pub fn revision(&self) -> &str {
        &self.revision
    }
}

/// All request-processing objects derived from one routing snapshot.
///
/// The Router and LLM facade resolver are shared across models, while every model owns its
/// immutable vLLM request processors and pinned revision.
pub struct RuntimeState {
    models: BTreeMap<String, ModelRuntime>,
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
            router,
            resolver,
        }
    }

    pub fn model(
        &self,
        model: &str,
        revision: Option<&str>,
    ) -> Result<&ModelRuntime, GenerationError> {
        let runtime = self
            .models
            .get(model)
            .ok_or(GenerationError::ModelNotFound)?;
        if revision.is_some_and(|revision| revision != runtime.revision) {
            return Err(GenerationError::ModelNotFound);
        }
        Ok(runtime)
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
}

impl Default for RuntimeGeneration {
    fn default() -> Self {
        Self::new()
    }
}

impl RuntimeGeneration {
    pub fn new() -> Self {
        Self {
            slot: ArcSwapOption::empty(),
        }
    }

    /// Publishes a newer serving generation. Candidate construction is serialized by cmd;
    /// the version guard prevents a stale mounted snapshot from replacing a newer generation.
    pub fn replace_state(
        &self,
        version: u64,
        state: Arc<RuntimeState>,
        control: Arc<dyn RuntimeControl>,
    ) -> bool {
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
        true
    }

    pub fn active_version(&self) -> Option<u64> {
        self.slot.load_full().map(|slot| slot.version)
    }

    pub async fn refresh_backend_readiness(&self) {
        if let Some(slot) = self.slot.load_full() {
            slot.control.refresh_backend_readiness().await;
        }
    }

    pub fn models(&self) -> Vec<String> {
        self.slot
            .load_full()
            .map(|slot| slot.control.healthy_models())
            .unwrap_or_default()
    }

    fn ready_state(&self) -> Result<Arc<RuntimeSlot>, GenerationError> {
        let slot = self.slot.load_full().ok_or(GenerationError::Unavailable)?;
        if slot.control.is_ready() {
            Ok(slot)
        } else {
            Err(GenerationError::Unavailable)
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
        let selection = slot
            .state
            .router
            .select(RouteContext {
                model: &request.model,
                revision: Some(&runtime.revision),
                required_capabilities: &request.required_capabilities,
                request: &generate_request,
            })
            .map_err(|_| GenerationError::Unavailable)?;
        let RouteSelection {
            decision,
            plan,
            reservation,
        } = selection;
        let facade = slot
            .state
            .resolver
            .resolve(&plan)
            .ok_or(GenerationError::Internal)?;
        let backend_stream = facade
            .generate(generate_request.clone())
            .await
            .map_err(GenerationError::from)?;
        let backend_stream =
            abort_on_drop(facade, generate_request.request_id.clone(), backend_stream);
        // Reservation and backend cancellation share the output stream lifetime.
        let stream: TokenStream = Box::pin(async_stream::stream! {
            let _reservation = reservation;
            let mut backend_stream = Box::pin(backend_stream);
            while let Some(item) = backend_stream.next().await { yield item; }
        });
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
        let slot = self.ready_state()?;
        let runtime = slot
            .state
            .model(&request.model, request.revision.as_deref())?;
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
        let slot = self.ready_state()?;
        let runtime = slot
            .state
            .model(&request.model, request.revision.as_deref())?;
        let (text_request, output_processor) = runtime
            .bundle
            .chat_processor
            .prepare(
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
        let runtime = slot.state.model(model, None)?;
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
        let runtime = slot.state.model(model, None)?;
        let parser = ParserSelection::None;
        let (text_request, _) = runtime
            .bundle
            .chat_processor
            .prepare(
                chat,
                NewChatOutputProcessorOptions {
                    tool_call_parser: &parser,
                    reasoning_parser: &parser,
                },
            )
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
        let runtime = slot.state.model(model, None)?;
        runtime
            .bundle
            .tokenizer
            .decode(token_ids, false)
            .map_err(|_| GenerationError::InvalidRequest)
    }

    fn ready(&self) -> bool {
        self.slot
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
            serving_ready: slot.control.is_ready(),
            active_generation: Some(slot.version),
            kv_index: slot.control.kv_index_diagnostics(),
        }
    }
}

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared generation contracts and immutable model state consumed by HTTP and runtime builders.

mod generation;
mod request;
mod timing;
pub(crate) mod workflow;

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use std::sync::atomic::AtomicU64;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use foretoken_admission::{AdmissionError, AdmissionPermit, AdmissionRequest};
use foretoken_chat::{ChatRequest, ChatRequestProcessor, DynChatOutputProcessor, ParserSelection};
use foretoken_llm_facade::{LlmFacadeError, LlmFacadeResolver, TokenStream};
use foretoken_router::{RouteDecision, RouteInventory, RouteTargetSet, Router};
use foretoken_text::{Prompt, SamplingParams, TextDecodeOptions, TextRequestProcessor};
use foretoken_tokenizer::DynTokenizer;
use serde::Serialize;
use thiserror::Error;

use crate::AdmissionOrigin;

pub use generation::RuntimeGeneration;
pub(crate) use timing::before_deadline;
use timing::before_waiting_deadline;

/// The tokenizer and chat renderer from one immutable model snapshot.
pub struct RuntimeBundle {
    pub text_processor: Arc<TextRequestProcessor>,
    pub tokenizer: DynTokenizer,
    pub chat_processor: Arc<ChatRequestProcessor>,
}

/// Input after HTTP has derived the request's routing and output-processing intent.
pub struct GenerationRequest {
    /// A batch child may already own its atomically reserved admission unit.
    pub admission: Option<AdmissionPermit>,
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
    /// Monotonic HTTP entry time shared by every candidate and execution stage.
    pub started_at: Instant,
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
    #[error("backend explicitly rejected acceptance because it is busy")]
    BackendBusy,
    #[error("backend rejected request")]
    BackendRejected,
    #[error("backend protocol failed")]
    BackendProtocol,
    #[error("request deadline exceeded")]
    DeadlineExceeded,
    #[error("generation service is overloaded")]
    Overloaded,
    #[error("admission queue timeout exceeded")]
    QueueTimeout,
    #[error("request fan-out exceeds configured admission capacity")]
    AdmissionCapacityExceeded,
    #[error("backend request failed")]
    RequestFailed,
    #[error("frontend internal error")]
    Internal,
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
    pub target_generation: Option<u64>,
    pub configuration_error: Option<String>,
    pub kv_index: KvIndexDiagnostics,
}

/// Request-scoped execution and budgets captured together before HTTP body extraction.
#[derive(Clone)]
pub struct RequestSnapshot {
    pub models: Arc<[String]>,
    pub generation: Arc<dyn Generation>,
    pub request_timeout: Duration,
    pub stream_idle: Duration,
}

/// Request-processing artifacts for one public model identity.
pub struct ModelRuntime {
    bundle: Arc<RuntimeBundle>,
    prefix_cache_isolation: Option<bool>,
}

// Preparation demand is attributed to eligible Pools in one service topology.
// This rotation only spreads observations across services; Admission alone owns queue order.
struct PreparationTargets {
    candidates: Vec<RouteTargetSet>,
    next: AtomicU64,
    _metrics: foretoken_metrics::QueueMetricsScope,
}

/// Backend dependencies for models whose engine owns video request preprocessing.
///
/// Keeping the model identities, route inventory, and HTTP client together makes video support an
/// all-or-nothing runtime capability instead of three independently initialized fields.
struct VideoBackendContext {
    models: BTreeSet<String>,
    inventory: Arc<dyn RouteInventory>,
    client: reqwest::Client,
}

/// All request-processing objects derived from one routing snapshot.
///
/// Models share the Router. Text models own immutable request processors; video models
/// delegate preprocessing to their selected HTTP backend.
pub struct RuntimeState {
    request_timeout: Duration,
    stream_idle: Duration,
    models: BTreeMap<String, ModelRuntime>,
    video: Option<VideoBackendContext>,
    preparation_targets: BTreeMap<String, PreparationTargets>,
    router: Arc<dyn Router>,
    resolver: Arc<dyn LlmFacadeResolver>,
}

/// Prepared settings and backend observations activated with one serving generation.
#[async_trait]
pub trait RuntimeControl: Send + Sync {
    /// Applies prepared process-local settings inside the generation publication boundary.
    fn activate(&self, version: u64);

    /// Refreshes backend and KV-index readiness for a candidate or published runtime generation.
    async fn refresh_backend_readiness(&self);

    /// Returns an owned list of model identities configured by this runtime control.
    fn configured_models(&self) -> Vec<String>;

    /// Reports whether the current generation can admit requests.
    ///
    /// Request admission and serving diagnostics consume this result until the next refresh or
    /// generation replacement.
    fn is_ready(&self) -> bool;

    /// Reports whether a configured model currently has a healthy serving path.
    ///
    /// Admission waits on this result before dispatching a model request; the default follows
    /// generation readiness for controls without per-model status.
    #[allow(unused_variables)]
    fn model_ready(&self, model: &str, allowed_pools: &[String]) -> bool {
        self.is_ready()
    }

    /// Returns an owned snapshot of the latest KV-index health for status and metrics consumers.
    fn kv_index_diagnostics(&self) -> KvIndexDiagnostics {
        KvIndexDiagnostics {
            state: "unavailable".into(),
            reason: Some("not_reported".into()),
            sources_healthy: 0,
            sources_total: 0,
        }
    }
}

/// Request execution and status consumed by the HTTP protocol adapters.
#[async_trait]
pub trait Generation: Send + Sync {
    /// Captures the active rules for one HTTP request; later updates do not alter its budgets.
    fn request_snapshot(&self) -> Result<RequestSnapshot, GenerationError>;

    /// Reserves a complete candidate batch before any child is submitted.
    async fn admit(&self, _request: &AdmissionRequest) -> Result<AdmissionPermit, GenerationError> {
        Ok(AdmissionPermit::default())
    }

    /// Dispatches a video request without text preprocessing; response ownership retains the route.
    async fn generate_video(
        &self,
        _request: crate::VideoRequest,
    ) -> Result<axum::response::Response, GenerationError> {
        Err(GenerationError::InvalidRequest)
    }

    /// Lowers and dispatches one completion request for HTTP completion handlers.
    ///
    /// Returns the routed backend stream and decoding inputs while the request remains owned by
    /// the response adapter, or a pre-stream error that can still become an HTTP response.
    async fn generate(&self, request: GenerationRequest) -> Result<Generated, GenerationError>;

    /// Lowers and dispatches one chat request for HTTP chat-completion handlers.
    ///
    /// Returns the routed stream with its chat output processor, which the response adapter owns
    /// until terminal output or disconnect; pre-stream failures remain typed HTTP errors.
    async fn generate_chat(
        &self,
        request: GenerationRequest,
        chat: ChatRequest,
        include_reasoning: bool,
    ) -> Result<GeneratedChat, GenerationError>;

    /// Tokenizes a completion prompt for the HTTP tokenization endpoint.
    ///
    /// Returns IDs, optional token strings, and the active model limit without starting backend
    /// work; implementations may reject this optional endpoint with `Internal`.
    async fn tokenize(
        &self,
        _model: &str,
        _prompt: Prompt,
        _add_special_tokens: bool,
        _return_token_strs: bool,
        _origin: AdmissionOrigin,
    ) -> Result<Tokenization, GenerationError> {
        Err(GenerationError::Internal)
    }
    /// Renders and tokenizes a chat prompt for the HTTP tokenization endpoint.
    ///
    /// Returns the resulting tokenization without dispatching a backend request; implementations
    /// that do not offer the endpoint return `Internal` before any request lifecycle begins.
    async fn tokenize_chat(
        &self,
        _model: &str,
        _chat: ChatRequest,
        _return_token_strs: bool,
        _origin: AdmissionOrigin,
    ) -> Result<Tokenization, GenerationError> {
        Err(GenerationError::Internal)
    }

    /// Decodes token IDs with the active model tokenizer for the HTTP detokenization endpoint.
    ///
    /// Returns prompt text without backend work, or `Internal` when that optional operation is not
    /// supported by the generation implementation.
    async fn detokenize(
        &self,
        _model: &str,
        _token_ids: &[u32],
        _origin: AdmissionOrigin,
    ) -> Result<String, GenerationError> {
        Err(GenerationError::Internal)
    }

    /// Reports whether the frontend is initialized and accepting HTTP traffic.
    ///
    /// Readiness probes use this independently of backend availability so unavailable models can
    /// return HTTP errors without removing the frontend from its Service.
    fn ready(&self) -> bool;

    /// Refreshes shared admission gauges for a metrics scrape, independently of routing decisions.
    async fn refresh_admission_observations(&self) {}

    /// Returns the current serving and KV-index status for HTTP status consumers.
    ///
    /// The default describes a generation without a published runtime; implementations update the
    /// values for the lifetime of their active control state.
    fn diagnostics(&self) -> RuntimeDiagnostics {
        RuntimeDiagnostics {
            serving_ready: self.ready(),
            active_generation: None,
            target_generation: None,
            configuration_error: None,
            kv_index: KvIndexDiagnostics {
                state: "unavailable".into(),
                reason: Some("no_active_generation".into()),
                sources_healthy: 0,
                sources_total: 0,
            },
        }
    }
}

impl RuntimeBundle {
    /// Groups processors resolved from one model snapshot for a model runtime.
    ///
    /// Runtime builders create the bundle once and request dispatch borrows its processors for the
    /// lifetime of that immutable generation.
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

impl ModelRuntime {
    /// Creates a per-model runtime retained inside one immutable runtime generation.
    ///
    /// Runtime builders supply the resolved bundle; request dispatch borrows the returned runtime
    /// until its containing generation is replaced.
    pub fn new(bundle: Arc<RuntimeBundle>, prefix_cache_isolation: Option<bool>) -> Self {
        Self {
            bundle,
            prefix_cache_isolation,
        }
    }
}

impl RuntimeState {
    /// Creates the immutable request-processing state for one prepared serving generation.
    ///
    /// Runtime builders supply model processors, router, and resolver; a published generation
    /// shares the returned state across requests until a newer snapshot replaces it.
    pub fn new(
        models: BTreeMap<String, ModelRuntime>,
        router: Arc<dyn Router>,
        resolver: Arc<dyn LlmFacadeResolver>,
        request_timeout: Duration,
        stream_idle: Duration,
    ) -> Self {
        Self {
            request_timeout,
            stream_idle,
            models,
            video: None,
            preparation_targets: BTreeMap::new(),
            router,
            resolver,
        }
    }

    /// Registers video models whose engine owns preprocessing, using the same route inventory.
    pub fn with_video_backend(
        mut self,
        models: BTreeSet<String>,
        inventory: Arc<dyn RouteInventory>,
        client: reqwest::Client,
    ) -> Self {
        self.video = Some(VideoBackendContext {
            models,
            inventory,
            client,
        });
        self
    }

    /// Registers complete Pool sets for attributing model-readiness waiting to autoscaling.
    /// Runtime builders supply these observations; they neither reserve capacity nor select a backend.
    pub fn with_preparation_targets(
        mut self,
        model: String,
        candidates: Vec<RouteTargetSet>,
    ) -> Self {
        if !candidates.is_empty() {
            self.preparation_targets.insert(
                model,
                PreparationTargets {
                    _metrics: foretoken_metrics::QueueMetricsScope::new(&candidates),
                    candidates,
                    next: AtomicU64::new(0),
                },
            );
        }
        self
    }

    /// Borrows the runtime used to admit and dispatch one model request, or returns `ModelNotFound`.
    pub fn model(&self, model: &str) -> Result<&ModelRuntime, GenerationError> {
        self.models.get(model).ok_or(GenerationError::ModelNotFound)
    }
}

impl From<AdmissionError> for GenerationError {
    fn from(error: AdmissionError) -> Self {
        match error {
            AdmissionError::Overloaded => Self::Overloaded,
            AdmissionError::QueueTimeout => Self::QueueTimeout,
            AdmissionError::DeadlineExceeded => Self::DeadlineExceeded,
            AdmissionError::BatchTooLarge => Self::AdmissionCapacityExceeded,
            AdmissionError::Closed | AdmissionError::StoreUnavailable => Self::Unavailable,
            AdmissionError::IdentityRequired => Self::InvalidRequest,
        }
    }
}

impl From<LlmFacadeError> for GenerationError {
    fn from(error: LlmFacadeError) -> Self {
        match error {
            LlmFacadeError::InvalidRequest => Self::InvalidRequest,
            LlmFacadeError::Busy => Self::BackendBusy,
            LlmFacadeError::Unavailable => Self::Unavailable,
            LlmFacadeError::Rejected => Self::BackendRejected,
            LlmFacadeError::Protocol => Self::BackendProtocol,
            LlmFacadeError::RequestFailed => Self::RequestFailed,
            LlmFacadeError::Configuration => Self::Internal,
        }
    }
}

impl From<foretoken_router::RouteError> for GenerationError {
    fn from(error: foretoken_router::RouteError) -> Self {
        match error {
            foretoken_router::RouteError::Busy => Self::BackendBusy,
            foretoken_router::RouteError::NoCompatibleTarget { .. }
            | foretoken_router::RouteError::NoMatchingDecode { .. } => Self::Unavailable,
            _ => Self::Internal,
        }
    }
}

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Owns immutable model runtimes and request dispatch.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use arc_swap::ArcSwapOption;
use async_trait::async_trait;
use foretoken_admission::{
    Admission, AdmissionApi, AdmissionAttempt, AdmissionContext, AdmissionError,
    AdmissionModelState, AdmissionModelStatus, AdmissionPermit, AdmissionRequest, AdmissionService,
    AdmissionStateReader, AdmissionTargetState, mark_request_deadline,
};
use foretoken_chat::{
    ChatRequest, ChatRequestProcessor, DynChatOutputProcessor, NewChatOutputProcessorOptions,
    ParserSelection,
};
use foretoken_llm_facade::{LlmFacadeError, LlmFacadeResolver, TokenStream};
use foretoken_router::{RouteDecision, RouteInventory, RouteTargetSet, Router, RouterRequest};
use foretoken_text::{
    Prompt, SamplingParams, TextDecodeOptions, TextRequest, TextRequestProcessor,
};
use foretoken_tokenizer::DynTokenizer;
use futures::StreamExt;
use serde::Serialize;
use thiserror::Error;

pub(crate) mod workflow;

use crate::{AdmissionOrigin, admission};
use workflow::execute_workflow;

/// The tokenizer and chat renderer from one immutable model snapshot.
pub struct RuntimeBundle {
    pub text_processor: Arc<TextRequestProcessor>,
    pub tokenizer: DynTokenizer,
    pub chat_processor: Arc<ChatRequestProcessor>,
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

/// Input after HTTP has derived the request's routing and output-processing intent.
pub struct GenerationRequest {
    /// A batch child may already own its atomically reserved admission unit.
    pub admission: Option<AdmissionPermit>,
    /// API origin and requested budget remain distinct from lowered execution parameters.
    pub api: Option<AdmissionApi>,
    pub requested_max_tokens: Option<u32>,
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
    #[error("request fan-out exceeds configured admission concurrency")]
    AdmissionCapacityExceeded,
    #[error("backend request failed")]
    RequestFailed,
    #[error("frontend internal error")]
    Internal,
}

impl From<AdmissionError> for GenerationError {
    fn from(error: AdmissionError) -> Self {
        match error {
            AdmissionError::Overloaded => Self::Overloaded,
            AdmissionError::QueueTimeout => Self::QueueTimeout,
            AdmissionError::DeadlineExceeded => Self::DeadlineExceeded,
            AdmissionError::BatchTooLarge => Self::AdmissionCapacityExceeded,
            AdmissionError::Closed => Self::Unavailable,
        }
    }
}

impl From<LlmFacadeError> for GenerationError {
    fn from(error: LlmFacadeError) -> Self {
        match error {
            LlmFacadeError::InvalidRequest => Self::InvalidRequest,
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
    fn model_ready(&self, model: &str) -> bool {
        self.is_ready()
    }

    /// Reads locally cached route metadata and load for admission without opening backend connections.
    fn route_target_states(&self, _model: &str, _window: Duration) -> Vec<AdmissionTargetState> {
        Vec::new()
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

#[async_trait]
pub trait Generation: Send + Sync {
    /// Returns the admission owner shared by HTTP and generation consumers, when supplied.
    fn admission(&self) -> Option<Arc<Admission>> {
        None
    }

    /// Exposes the existing total budget to HTTP intake before body extraction.
    fn request_timeout(&self) -> Option<Duration> {
        None
    }

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

    /// Returns the current serving and KV-index status for HTTP status consumers.
    ///
    /// The default describes a generation without a published runtime; implementations update the
    /// values for the lifetime of their active control state.
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
    /// Creates a per-model runtime retained inside one immutable runtime generation.
    ///
    /// Runtime builders supply the resolved bundle; request dispatch borrows the returned runtime
    /// until its containing generation is replaced.
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
    models: BTreeMap<String, ModelRuntime>,
    video: Option<VideoBackendContext>,
    admission_targets: BTreeMap<String, AdmissionTargets>,
    router: Arc<dyn Router>,
    resolver: Arc<dyn LlmFacadeResolver>,
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
    ) -> Self {
        Self {
            models,
            video: None,
            admission_targets: BTreeMap::new(),
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

    /// Attaches the controller-selected admission target sets for one model.
    ///
    /// Runtime builders call this while assembling state. The returned state selects one target
    /// per queued request until the generation is replaced; empty candidate sets are ignored.
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

    /// Borrows the runtime used to admit and dispatch one model request, or returns `ModelNotFound`.
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
    admission: Arc<Admission>,
}

impl RuntimeGeneration {
    /// Creates the long-lived owner of controller-published runtime generations.
    ///
    /// The frontend process retains the returned owner for its full lifetime; snapshot watchers
    /// publish into it and HTTP handlers load its current immutable state per request.
    pub fn new(request_timeout: Duration, admission: Arc<Admission>) -> Self {
        let (publication_updates, _) = tokio::sync::watch::channel(0);
        Self {
            slot: ArcSwapOption::empty(),
            publication: Mutex::new(()),
            publication_updates,
            accepting: AtomicBool::new(true),
            request_timeout,
            admission,
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

    /// Stops accepting new requests while retaining the active generation for draining streams.
    ///
    /// Process shutdown calls this once before stopping HTTP; it wakes admission waiters, which
    /// then return unavailable while requests already holding a runtime can finish.
    pub fn close_admission(&self) {
        self.accepting.store(false, Ordering::Release);
        self.admission.rule().close();
        self.publication_updates
            .send_replace(self.slot.load_full().map_or(0, |slot| slot.version));
    }

    /// Returns the version of the generation currently published by the snapshot watcher.
    ///
    /// Snapshot processing uses the optional value to reject stale candidates. `None` indicates
    /// that the frontend has not yet published a serving generation.
    pub fn active_version(&self) -> Option<u64> {
        self.slot.load_full().map(|slot| slot.version)
    }

    /// Refreshes readiness for the currently loaded runtime slot and wakes queued admission checks.
    pub async fn refresh_backend_readiness(&self) {
        if let Some(slot) = self.slot.load_full() {
            slot.control.refresh_backend_readiness().await;
            self.publication_updates.send_replace(slot.version);
        }
    }

    /// Returns an owned model list for the currently loaded runtime slot, or an empty list before publication.
    pub fn configured_models(&self) -> Vec<String> {
        self.slot
            .load_full()
            .map(|slot| slot.control.configured_models())
            .unwrap_or_default()
    }

    /// Enforces the request budget for every rule, including waits without HTTP intake reservations.
    /// Live observations are provided here; unavailable service claims stay absent.
    async fn acquire_admission(
        &self,
        request: &AdmissionRequest,
    ) -> Result<AdmissionPermit, GenerationError> {
        let deadline = tokio::time::Instant::from_std(request.received_at + self.request_timeout);
        let attempt = AdmissionAttempt::work(deadline);
        let context = AdmissionContext {
            deadline,
            service: AdmissionService::default(),
            state: self,
            queue: attempt.queue(),
        };
        attempt
            .run(self.admission.rule().admit(request, &context))
            .await
            .map_err(GenerationError::from)
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

    async fn generation_slot(
        &self,
        model: &str,
        wait_for_ready: bool,
    ) -> Result<Arc<RuntimeSlot>, GenerationError> {
        // A configured model without a prepared processor is admission-only: keep its targets
        // queued while waiting, then reload the complete slot across each generation boundary.
        let mut publication_updates = self.publication_updates.subscribe();
        let mut queued = None;
        loop {
            let slot = self.ready_state()?;
            if (slot.state.models.contains_key(model)
                || slot
                    .state
                    .video
                    .as_ref()
                    .is_some_and(|video| video.models.contains(model)))
                && slot.control.model_ready(model)
            {
                return Ok(slot);
            }
            let Some(targets) = slot.state.select_admission_targets(model) else {
                return Err(GenerationError::ModelNotFound);
            };
            if !wait_for_ready {
                return Err(GenerationError::Unavailable);
            }
            if queued.is_none() {
                queued = Some(foretoken_metrics::QueueGuard::runtime_preparation(&targets));
            }
            if publication_updates.changed().await.is_err()
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
        admission: AdmissionPermit,
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
        // Tokenization is synchronous; do not admit backend work if it exhausted the budget.
        if Instant::now() >= request.started_at + self.request_timeout {
            return Err(GenerationError::DeadlineExceeded);
        }
        let generate_request = prepared.generate_request;
        let context = RouterRequest::new(request.model.clone(), Arc::new(generate_request.clone()));
        let mut session = slot.state.router.start(context).await;
        let initial = session
            .select_initial()
            .map_err(|_| GenerationError::Unavailable)?;
        let _queue = foretoken_metrics::QueueGuard::backend_dispatch(&initial.admission_targets);
        let (decision, backend_stream) = execute_workflow(
            &*slot.state.resolver,
            &mut *session,
            initial,
            generate_request.clone(),
        )
        .await?;
        // The response stream owns the work permit and routing load through completion or cancellation.
        let stream = Box::pin(async_stream::stream! {
            use futures::StreamExt;
            let _admission = admission;
            let mut backend_stream = backend_stream;
            let mut response_started = false;
            while let Some(output) = backend_stream.next().await {
                if !response_started {
                    session.response_started();
                    response_started = true;
                }
                let terminal = output.as_ref().map_or(true, |output| output.finish_reason.is_some());
                if terminal { session.stage_complete(); }
                yield output;
                if terminal { break; }
            }
            session.stage_complete();
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

/// Keeps preprocessing accounted for when an HTTP future is canceled.
/// Upstream image processing awaits blocking work that cannot be canceled once started;
/// a protected task retains the permit until that work finishes, without dispatching inference.
async fn prepare_with_permit<T: Send + 'static>(
    admission: AdmissionPermit,
    work: impl std::future::Future<Output = Result<T, GenerationError>> + Send + 'static,
) -> Result<(T, AdmissionPermit), GenerationError> {
    let reserved = admission.is_reserved();
    let preparation = async move {
        let result = work.await?;
        Ok((result, admission))
    };
    if reserved {
        tokio::spawn(preparation)
            .await
            .map_err(|_| GenerationError::Internal)?
    } else {
        preparation.await
    }
}

/// Bounds preparation and workflow execution by the original HTTP request deadline.
///
/// Timed-out workflows drop their backend cleanup guards. Protected preprocessing retains
/// its separate permit until any non-cancelable blocking work has completed.
pub(crate) async fn before_deadline<T>(
    deadline: tokio::time::Instant,
    work: impl std::future::Future<Output = Result<T, GenerationError>>,
) -> Result<T, GenerationError> {
    tokio::select! {
        biased;
        _ = tokio::time::sleep_until(deadline) => {
            mark_request_deadline();
            Err(GenerationError::DeadlineExceeded)
        },
        result = work => {
            // Synchronous tokenization can finish without yielding to the timer.
            if tokio::time::Instant::now() >= deadline {
                mark_request_deadline();
                Err(GenerationError::DeadlineExceeded)
            } else {
                result
            }
        }
    }
}

/// Transfers the same request deadline to the output stream owned by protocol adapters.
///
/// The task owns backend cleanup even while HTTP backpressure stops polling the returned stream.
/// A bounded channel preserves backpressure; dropping its receiver cancels the task's backend work.
fn deadline_stream(mut stream: TokenStream, deadline: tokio::time::Instant) -> TokenStream {
    let (sender, mut receiver) = tokio::sync::mpsc::channel(1);
    let (expiry_sender, expiry_receiver) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let expired = tokio::select! {
            biased;
            _ = sender.closed() => false,
            _ = tokio::time::sleep_until(deadline) => true,
            _ = async {
                while let Some(item) = stream.next().await {
                    let finished = item.as_ref().map_or(true, |output| output.finish_reason.is_some());
                    if sender.send(item).await.is_err() || finished {
                        break;
                    }
                }
            } => false,
        };
        // Release backend work and finish without waiting for a stalled HTTP reader. Expiry has
        // a separate terminal slot so the queued token remains ordered before the timeout error.
        drop(stream);
        if expired {
            let _ = expiry_sender.send(());
        }
    });
    Box::pin(async_stream::stream! {
        while let Some(item) = receiver.recv().await {
            yield item;
        }
        if expiry_receiver.await.is_ok() {
            yield Err(LlmFacadeError::RequestFailed);
        }
    })
}

impl AdmissionStateReader for RuntimeGeneration {
    fn model_state(&self, model: &str, window: Duration) -> Option<AdmissionModelState> {
        let slot = self.slot.load_full()?;
        let runtime = slot.state.models.get(model);
        let has_runtime = runtime.is_some()
            || slot
                .state
                .video
                .as_ref()
                .is_some_and(|video| video.models.contains(model));
        let status = if !slot
            .control
            .configured_models()
            .iter()
            .any(|name| name == model)
        {
            AdmissionModelStatus::Unknown
        } else if !has_runtime {
            AdmissionModelStatus::Preparing
        } else if slot.control.is_ready() && slot.control.model_ready(model) {
            AdmissionModelStatus::Ready
        } else {
            AdmissionModelStatus::Unavailable
        };
        Some(AdmissionModelState {
            status,
            max_model_len: runtime.map(|runtime| runtime.bundle.text_processor.max_model_len()),
            targets: slot.control.route_target_states(model, window),
            observed_at: Instant::now(),
        })
    }
}

#[async_trait]
impl Generation for RuntimeGeneration {
    fn admission(&self) -> Option<Arc<Admission>> {
        Some(self.admission.clone())
    }

    fn request_timeout(&self) -> Option<Duration> {
        Some(self.request_timeout)
    }

    async fn admit(&self, request: &AdmissionRequest) -> Result<AdmissionPermit, GenerationError> {
        // Validate the model without retaining its runtime while waiting. Admission
        // limits are frontend pressure, not additional ModelPool scheduler demand.
        if self.admission.rule().requires_ready_runtime() {
            let slot = self.ready_state()?;
            if !slot.state.models.contains_key(&request.model) {
                return Err(
                    if slot.state.admission_targets.contains_key(&request.model) {
                        GenerationError::Unavailable
                    } else {
                        GenerationError::ModelNotFound
                    },
                );
            }
        }
        self.acquire_admission(request).await
    }

    async fn generate_video(
        &self,
        request: crate::VideoRequest,
    ) -> Result<axum::response::Response, GenerationError> {
        let slot = self.generation_slot(&request.model, true).await?;
        let video = slot
            .state
            .video
            .as_ref()
            .ok_or(GenerationError::Unavailable)?;
        if !video.models.contains(&request.model) {
            return Err(GenerationError::InvalidRequest);
        }
        let context = RouterRequest::video(request.model.clone(), request.request_id.clone());
        let mut session = slot.state.router.start(context).await;
        let decision = session
            .select_initial()
            .map_err(|_| GenerationError::Unavailable)?;
        let endpoint = video
            .inventory
            .http_endpoint(&decision)
            .ok_or(GenerationError::Unavailable)?;
        let _queue = foretoken_metrics::QueueGuard::backend_dispatch(&decision.admission_targets);
        crate::video::forward(
            &video.client,
            &endpoint,
            request,
            session,
            self.request_timeout,
        )
        .await
    }

    async fn generate(&self, mut request: GenerationRequest) -> Result<Generated, GenerationError> {
        let deadline = tokio::time::Instant::from_std(request.started_at + self.request_timeout);
        let mut generated = before_deadline(deadline, async {
            let admission = match request.admission.take() {
                Some(permit) => permit,
                None => self.admit(&admission::generation(&request, None)).await?,
            };
            let slot = self
                .generation_slot(
                    &request.model,
                    !self.admission.rule().requires_ready_runtime(),
                )
                .await?;
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
            self.dispatch(slot.clone(), runtime, request, text_request, admission)
                .await
        })
        .await?;
        generated.routed.stream = deadline_stream(generated.routed.stream, deadline);
        Ok(generated)
    }

    async fn generate_chat(
        &self,
        mut request: GenerationRequest,
        chat: ChatRequest,
        include_reasoning: bool,
    ) -> Result<GeneratedChat, GenerationError> {
        let deadline = tokio::time::Instant::from_std(request.started_at + self.request_timeout);
        let mut chat = before_deadline(deadline, async {
            let admission = match request.admission.take() {
                Some(permit) => permit,
                None => {
                    self.admit(&admission::generation(&request, Some(&chat)))
                        .await?
                }
            };
            let slot = self
                .generation_slot(
                    &request.model,
                    !self.admission.rule().requires_ready_runtime(),
                )
                .await?;
            let runtime = slot.state.model(&request.model)?;
            let bundle = runtime.bundle.clone();
            let tool_parser = request.tool_call_parser.clone();
            let reasoning_parser = request.reasoning_parser.clone();
            let ((mut text_request, output_processor), admission) =
                prepare_with_permit(admission, async move {
                    bundle
                        .chat_processor
                        .prepare_with_options(
                            chat,
                            NewChatOutputProcessorOptions {
                                tool_call_parser: &tool_parser,
                                reasoning_parser: &reasoning_parser,
                            },
                        )
                        .await
                        .map_err(|error| {
                            if error.is_request_validation_error() {
                                GenerationError::InvalidRequest
                            } else {
                                GenerationError::Internal
                            }
                        })
                })
                .await?;
            // The chat renderer stamps its own entry time after runtime admission. Preserve the
            // HTTP handler's earlier origin so chat and text requests include the same wait.
            text_request.arrival_time = request.arrival_time.or(text_request.arrival_time);
            let generated = self
                .dispatch(slot.clone(), runtime, request, text_request, admission)
                .await?;
            Ok(GeneratedChat {
                generated,
                output_processor,
                include_reasoning,
            })
        })
        .await?;
        chat.generated.routed.stream = deadline_stream(chat.generated.routed.stream, deadline);
        Ok(chat)
    }

    async fn tokenize(
        &self,
        model: &str,
        prompt: Prompt,
        add_special_tokens: bool,
        return_token_strs: bool,
        origin: AdmissionOrigin,
    ) -> Result<Tokenization, GenerationError> {
        let _admission = self
            .acquire_admission(&admission::tokenize(model, &prompt, origin))
            .await?;
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
        origin: AdmissionOrigin,
    ) -> Result<Tokenization, GenerationError> {
        let admission = self
            .acquire_admission(&admission::tokenize_chat(model, &chat, origin))
            .await?;
        let bundle = self.ready_state()?.state.model(model)?.bundle.clone();
        let (tokens, _admission) = prepare_with_permit(admission, async move {
            let text_request = bundle
                .chat_processor
                .prepare_for_tokenization(chat)
                .await
                .map_err(|_| GenerationError::InvalidRequest)?;
            let prepared = bundle
                .text_processor
                .prepare(text_request)
                .map_err(|_| GenerationError::InvalidRequest)?;
            let token_ids = prepared.generate_request.prompt_token_ids;
            let tokenizer = &bundle.tokenizer;
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
                max_model_len: bundle.text_processor.max_model_len(),
            })
        })
        .await?;
        Ok(tokens)
    }

    async fn detokenize(
        &self,
        model: &str,
        token_ids: &[u32],
        origin: AdmissionOrigin,
    ) -> Result<String, GenerationError> {
        let _admission = self
            .acquire_admission(&admission::detokenize(model, token_ids.len(), origin))
            .await?;
        let slot = self.ready_state()?;
        let runtime = slot.state.model(model)?;
        runtime
            .bundle
            .tokenizer
            .decode(token_ids, false)
            .map_err(|_| GenerationError::InvalidRequest)
    }

    fn ready(&self) -> bool {
        self.accepting.load(Ordering::Acquire) && self.slot.load().is_some()
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

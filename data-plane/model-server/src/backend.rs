// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine-neutral HTTP-facing boundary and the thin vLLM EngineCore adapter.

use async_trait::async_trait;
use foretoken_model_protocol::SchedulerLoad;
use futures::{Stream, StreamExt};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Instant;
use thiserror::Error;
use tokio::sync::{RwLock, mpsc};
use tokio_util::sync::CancellationToken;
use vllm_engine_core_client::protocol::structured_outputs::StructuredOutputBackend;
use vllm_llm::{FinishReason, Llm};
use vllm_metrics::{EngineLabels, METRICS};

use crate::backend_telemetry::{BoundaryLatencyMetrics, read_vllm_metrics};

pub use foretoken_model_protocol::{
    CumulativeHistogram, GenerateInput, TokenErrorCode, TokenEvent, TokenOutput,
};

/// Engine-neutral output stream consumed by internal HTTP handlers.
pub type TokenStream = Pin<Box<dyn Stream<Item = Result<TokenEvent, BackendError>> + Send>>;

/// Output delivery and engine-origin termination have separate lifetimes.
/// The API execution owner retains completion even after its HTTP consumer disconnects.
pub struct BackendGeneration {
    pub stream: TokenStream,
    pub completion: Pin<Box<dyn Future<Output = Result<(), BackendError>> + Send>>,
}

/// 将一个已接纳请求的准备、取消与原生提交串行化；API 与 backend 共用该所有者。
pub struct ExecutionControl {
    pub(crate) owner: String,
    phase: Mutex<ExecutionPhase>,
    cancellation: CancellationToken,
}

impl Default for ExecutionControl {
    fn default() -> Self {
        Self {
            owner: uuid::Uuid::new_v4().to_string(),
            phase: Mutex::default(),
            cancellation: CancellationToken::new(),
        }
    }
}

#[derive(Default)]
enum ExecutionPhase {
    #[default]
    Preparing,
    Submitted,
    Cancelled,
}

impl ExecutionControl {
    /// 接收 abort 或断连信号；准备阶段的取消阻止后续原生提交。
    pub fn cancel(&self) {
        let mut phase = self.phase.lock().expect("execution phase lock poisoned");
        if matches!(*phase, ExecutionPhase::Preparing) {
            *phase = ExecutionPhase::Cancelled;
        }
        self.cancellation.cancel();
    }

    /// 由接收执行等待取消，原生提交后的实际终止仍由 completion 确认。
    pub async fn cancelled(&self) {
        self.cancellation.cancelled().await;
    }

    /// 供尚未提交的等待阶段停止推进；提交后的终止仍需原生 completion。
    pub fn is_cancelled(&self) -> bool {
        self.cancellation.is_cancelled()
    }

    /// 标记不可重放的原生提交起点，与准备阶段的取消共享一个原子边界。
    fn begin_submission(&self) -> bool {
        let mut phase = self.phase.lock().expect("execution phase lock poisoned");
        if !matches!(*phase, ExecutionPhase::Preparing) {
            return false;
        }
        *phase = ExecutionPhase::Submitted;
        true
    }

    /// 供 API 的错误收尾区分明确未提交与结果未知的原生执行。
    pub fn submitted(&self) -> bool {
        matches!(
            *self.phase.lock().expect("execution phase lock poisoned"),
            ExecutionPhase::Submitted
        )
    }
}

/// Cumulative backend observations included in a telemetry snapshot.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct BackendTelemetry {
    pub data_parallel_ranks: Vec<foretoken_model_protocol::DataParallelTelemetry>,
    pub request_cost: foretoken_model_protocol::RequestCostTelemetry,
    pub max_concurrent_requests: Option<u64>,
    pub scheduler_running_requests: Option<u64>,
    pub scheduler_waiting_requests: Option<u64>,
    pub kv_cache_usage: Option<f64>,
    pub prompt_tokens_total: Option<u64>,
    pub generation_tokens_total: Option<u64>,
    pub ttft_seconds: CumulativeHistogram,
    pub tpot_seconds: CumulativeHistogram,
    pub e2e_seconds: CumulativeHistogram,
}

/// Backend failures classified without retaining vLLM's diagnostic text.
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum BackendError {
    #[error("request was rejected")]
    Rejected,
    #[error("request is invalid")]
    InvalidRequest,
    #[error("backend is unavailable")]
    Unavailable,
    #[error("backend protocol failed")]
    Protocol,
    #[error("backend request failed")]
    RequestFailed,
}

/// Backend-neutral failure to encode the active inference engine's metrics.
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
#[error("backend metrics rendering failed")]
pub struct MetricsError;

impl BackendError {
    /// Maps a boundary failure to the terminal token code emitted to internal stream consumers.
    ///
    /// The returned wire value carries no borrowed backend diagnostics.
    pub const fn token_error_code(self) -> TokenErrorCode {
        match self {
            Self::Unavailable => TokenErrorCode::Unavailable,
            Self::Rejected | Self::InvalidRequest | Self::Protocol => TokenErrorCode::Protocol,
            Self::RequestFailed => TokenErrorCode::RequestFailed,
        }
    }

    fn from_llm(error: vllm_llm::Error) -> Self {
        match error {
            vllm_llm::Error::EmptyPromptTokenIds { .. } => Self::InvalidRequest,
            vllm_llm::Error::EngineCoreClient(error) => Self::from_engine_client(error),
        }
    }

    fn from_engine_client(error: vllm_engine_core_client::Error) -> Self {
        use vllm_engine_core_client::Error;

        match error {
            Error::EngineCoreDead
            | Error::ClientClosed { .. }
            | Error::ControlClosed { .. }
            | Error::DispatcherClosed { .. }
            | Error::HandshakeTimeout { .. }
            | Error::InputRegistrationTimeout { .. }
            | Error::Io(_)
            | Error::RequestStreamClosed { .. }
            | Error::Transport(_)
            | Error::ZmqRuntimeTask(_) => Self::Unavailable,
            Error::Encode { .. }
            | Error::Decode { .. }
            | Error::ExtValueDecode { .. }
            | Error::UnexpectedCoordinatorOutput { .. }
            | Error::UnexpectedDispatcherOutput { .. }
            | Error::UnexpectedHandshakeIdentity { .. }
            | Error::UnexpectedHandshakeMessage { .. }
            | Error::UnsupportedAuxFrames { .. }
            | Error::UnsupportedCoordinatorEngineId { .. }
            | Error::UnsupportedExternalCoordinator
            | Error::ValueDecode(_) => Self::Protocol,
            Error::UnsupportedField { .. } => Self::InvalidRequest,
            Error::DuplicateRequestId { .. }
            | Error::InvalidDataParallelRank { .. }
            | Error::InvalidStructuredOutputsParams { .. } => Self::Rejected,
            Error::UtilityCallClosed { .. }
            | Error::UtilityCallFailed { .. }
            | Error::UtilityResultDecode { .. }
            | Error::InconsistentUtilityResults { .. }
            | Error::Shared(_) => Self::RequestFailed,
        }
    }
}

/// Minimal inference backend operations that this group-local server needs.
#[async_trait]
pub trait Backend: Send + Sync {
    /// Submits one request and returns separate output and engine-completion ownership to the API task.
    async fn generate(
        &self,
        request: GenerateInput,
        execution: Arc<ExecutionControl>,
    ) -> Result<BackendGeneration, BackendError>;

    /// Cancels request IDs supplied by the abort handler; backend ownership remains unchanged.
    async fn abort(&self, request_ids: &[String]) -> Result<(), BackendError>;

    /// Observes reusable prefixes in every local scheduler without allocating request capacity.
    async fn local_prefixes(
        &self,
        request: &foretoken_model_protocol::KvPrefixRequest,
    ) -> Result<Vec<foretoken_model_protocol::KvRankPrefix>, BackendError>;

    /// 查询各 DP scheduler 保留的 Decode 前缀，不领取或保留新的 KV。
    async fn retained_prefixes(
        &self,
        request: &foretoken_model_protocol::KvPrefixRequest,
    ) -> Result<Vec<foretoken_model_protocol::KvRetainedRankPrefix>, BackendError>;

    /// 在原生来源处原子领取，只有实际 worker 保留成功才返回 Granted。
    async fn claim_kv_prefix(
        &self,
        request: &foretoken_model_protocol::KvPrefixClaim,
    ) -> Result<foretoken_model_protocol::KvPrefixClaimResult, BackendError>;

    /// 撤销未开始的读取；InUse 表示原生传输仍拥有块引用。
    async fn cancel_kv_prefix(
        &self,
        request: &foretoken_model_protocol::KvPrefixCancel,
    ) -> Result<foretoken_model_protocol::KvPrefixCancelResult, BackendError>;

    /// 关闭新领取并返回尚未排空的原生传输数，供进程监督退出使用。
    async fn drain_kv_transfers(&self) -> Result<u64, BackendError>;

    /// Returns a snapshot published by telemetry handlers without transferring backend ownership.
    fn telemetry(&self) -> BackendTelemetry;

    /// Reads scheduler-owned token demand for the optional autoscaling observation endpoint.
    /// Failure leaves ordinary inference and telemetry available without inventing zero load.
    async fn scheduler_load(&self) -> Result<Vec<SchedulerLoad>, BackendError>;

    /// Renders the OpenMetrics payload published by the metrics handler as an owned response body.
    fn render_openmetrics(&self) -> Result<String, MetricsError>;
}

/// vLLM adapter that retains its public `Llm` facade rather than its wire protocol.
pub struct VllmBackend {
    llm: RwLock<Option<Llm>>,
    closing: CancellationToken,
    max_concurrent_requests: Option<u64>,
    engine_labels: Vec<EngineLabels>,
    boundary_latency: Arc<Mutex<BoundaryLatencyMetrics>>,
    bidirectional_kv_xfer: bool,
    kv_connector_utility: bool,
    kv_client: reqwest::Client,
    kv_cleanup: tokio_util::task::TaskTracker,
}

impl VllmBackend {
    /// Creates the model-server adapter and retains the provided vLLM `Llm` facade until shutdown.
    pub fn new(
        llm: Llm,
        max_concurrent_requests: Option<u64>,
        bidirectional_kv_xfer: bool,
        kv_connector_utility: bool,
    ) -> Self {
        let client = llm.engine_core_client();
        let model_name = client.model_name().to_string();
        let engine_labels = client
            .engine_indices()
            .into_iter()
            .map(|engine| EngineLabels {
                model_name: model_name.clone(),
                engine,
            })
            .collect();
        Self {
            llm: RwLock::new(Some(llm)),
            closing: CancellationToken::new(),
            max_concurrent_requests,
            engine_labels,
            boundary_latency: Arc::new(Mutex::new(BoundaryLatencyMetrics::new())),
            bidirectional_kv_xfer,
            kv_connector_utility,
            kv_client: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(foretoken_model_protocol::KV_OBSERVATION_TIMEOUT)
                .build()
                .expect("static KV client configuration"),
            kv_cleanup: tokio_util::task::TaskTracker::new(),
        }
    }

    /// 通过已有 EngineCore utility 读取各 rank 的 connector 结果，不新建控制通道。
    async fn kv_utility<T: serde::de::DeserializeOwned>(
        &self,
        operation: &str,
        payload: serde_json::Value,
    ) -> Result<Vec<T>, BackendError> {
        let guard = self.llm.read().await;
        let client = guard
            .as_ref()
            .ok_or(BackendError::Unavailable)?
            .engine_core_client();
        let results =
            self.closing
                .run_until_cancelled(client.call_utility::<serde_json::Value, _>(
                    "kv_connector_utility",
                    (operation, payload),
                ))
                .await
                .ok_or(BackendError::Unavailable)?
                .map_err(BackendError::from_engine_client)?;
        results
            .into_iter()
            .map(|value| serde_json::from_value(value).map_err(|_| BackendError::Protocol))
            .collect()
    }

    /// Takes and shuts down the owned vLLM `Llm` facade during model-server teardown.
    pub async fn shutdown(&self) -> Result<(), BackendError> {
        // Pending submissions and utilities hold read guards. Release them before taking
        // ownership of the client that broadcasts terminal errors to active streams.
        self.closing.cancel();
        let Some(llm) = self.llm.write().await.take() else {
            return Ok(());
        };
        llm.shutdown().await.map_err(BackendError::from_llm)
    }

    /// 监督者在计算排空后等待远端领取清理；本地引擎退役不结束这项责任。
    pub async fn wait_for_kv_cleanup(&self) {
        self.kv_cleanup.close();
        self.kv_cleanup.wait().await;
    }

    /// 返回退出预算到期时尚未确认的远端领取数，供监督者报告未完成的清理。
    pub fn pending_kv_cleanup(&self) -> usize {
        self.kv_cleanup.len()
    }

    /// Runs the MetaX worker capture method on every rank; the supervisor owns its deadline.
    pub async fn set_mctracer(
        &self,
        start: bool,
        directory: &std::path::Path,
    ) -> Result<(), String> {
        let guard = self.llm.read().await;
        let client = guard
            .as_ref()
            .ok_or("engine is unavailable")?
            .engine_core_client();
        self.closing
            .run_until_cancelled(client.collective_rpc(
                "foretoken_mctracer",
                None,
                (start, directory.to_string_lossy()),
                std::collections::BTreeMap::<String, String>::new(),
            ))
            .await
            .ok_or("engine is shutting down")?
            .map(|_| ())
            .map_err(|error| error.to_string())
    }

    /// Controls native profiling across every rank using the supervisor's trace prefix.
    /// The supervisor owns the output directory, deadlines, and failure escalation.
    pub async fn set_profiling(&self, start: bool, prefix: &str) -> Result<(), String> {
        let guard = self.llm.read().await;
        let client = guard
            .as_ref()
            .ok_or("engine is unavailable")?
            .engine_core_client();
        let result = self
            .closing
            .run_until_cancelled(async {
                if start {
                    client.start_profile(Some(prefix)).await
                } else {
                    client.stop_profile(Some(prefix)).await
                }
            })
            .await
            .ok_or("engine is shutting down")?;
        result.map_err(|error| error.to_string())
    }
}

/// Restore external request identity and track engine-boundary latency without consumer delay.
fn tracked_stream<S>(
    stream: S,
    request_id: String,
    started_at: Instant,
    boundary_latency: Arc<Mutex<BoundaryLatencyMetrics>>,
) -> TokenStream
where
    S: Stream<Item = Result<vllm_llm::GenerateOutput, vllm_llm::Error>> + Send + 'static,
{
    // Relay vLLM output through a bounded task so the returned HTTP stream owns cancellation while
    // the adapter records only engine-boundary latency, not downstream consumer backpressure.
    let (sender, mut receiver) = mpsc::channel(1);
    tokio::spawn(async move {
        let mut first_token_at = None;
        let mut generation_tokens = 0_u64;
        let mut response_backpressured = false;
        let mut stream = Box::pin(stream);
        loop {
            let item = tokio::select! {
                _ = sender.closed() => return,
                item = stream.next() => item,
            };
            let Some(item) = item else {
                return;
            };
            let (event, terminal) = match item {
                Ok(output) if output.finish_reason == Some(FinishReason::Error) => (
                    Ok(TokenEvent::Error {
                        request_id: request_id.clone(),
                        code: TokenErrorCode::RequestFailed,
                    }),
                    true,
                ),
                Ok(mut output) => {
                    let now = Instant::now();
                    generation_tokens += output.token_ids.len() as u64;
                    if first_token_at.is_none() && !output.token_ids.is_empty() {
                        first_token_at = Some(now);
                        if !response_backpressured {
                            boundary_latency
                                .lock()
                                .expect("boundary latency metrics lock poisoned")
                                .observe_ttft(now.duration_since(started_at).as_secs_f64());
                        }
                    }
                    let finish_reason = output.finish_reason.clone();
                    let terminal = finish_reason.is_some();
                    let successful_terminal = matches!(
                        finish_reason,
                        Some(FinishReason::Stop(_))
                            | Some(FinishReason::Length)
                            | Some(FinishReason::Repetition(_))
                    );
                    if successful_terminal && !response_backpressured {
                        let mut metrics = boundary_latency
                            .lock()
                            .expect("boundary latency metrics lock poisoned");
                        metrics.observe_e2e(now.duration_since(started_at).as_secs_f64());
                        if let Some(first_token_at) = first_token_at
                            && generation_tokens > 1
                        {
                            metrics.observe_tpot(
                                now.duration_since(first_token_at).as_secs_f64()
                                    / (generation_tokens - 1) as f64,
                            );
                        }
                    }
                    output.request_id.clone_from(&request_id);
                    (Ok(TokenEvent::Token(Box::new(output.into()))), terminal)
                }
                Err(error) => (Err(BackendError::from_llm(error)), true),
            };
            match sender.try_send(event) {
                Ok(()) => {}
                Err(mpsc::error::TrySendError::Full(event)) => {
                    response_backpressured = true;
                    if sender.send(event).await.is_err() {
                        return;
                    }
                }
                Err(mpsc::error::TrySendError::Closed(_)) => return,
            }
            if terminal {
                return;
            }
        }
    });

    Box::pin(async_stream::stream! {
        while let Some(event) = receiver.recv().await {
            yield event;
        }
    })
}

#[async_trait]
impl Backend for VllmBackend {
    async fn generate(
        &self,
        mut request: GenerateInput,
        execution: Arc<ExecutionControl>,
    ) -> Result<BackendGeneration, BackendError> {
        // HTTP decoding discards backend hints; headless EngineCore skips frontend
        // validation and retains its first grammar backend. Use Xgrammar consistently
        // so JSON and structural-tag requests work regardless of arrival order.
        if let Some(outputs) = &mut request.sampling_params.structured_outputs {
            outputs.backend = StructuredOutputBackend::Xgrammar;
        }
        let started_at = Instant::now();
        // 来源是可选加速提示；实例更换后配置若已不适用，仍执行普通 Prefill。
        let source_read = if let Some(source) = request
            .kv_reuse
            .take()
            .filter(|_| self.bidirectional_kv_xfer)
        {
            let mut read = crate::kv_reuse::RemoteKvRead::new(
                source,
                execution.owner.clone(),
                self.kv_client.clone(),
                self.kv_cleanup.clone(),
            );
            if self
                .closing
                .run_until_cancelled(read.prepare(&mut request, &execution))
                .await
                .ok_or(BackendError::Unavailable)??
            {
                Some(read)
            } else {
                None
            }
        } else {
            None
        };
        let guard = self.llm.read().await;
        let llm = guard.as_ref().ok_or(BackendError::Unavailable)?;
        if !execution.begin_submission() {
            return Err(BackendError::Rejected);
        }
        let request_id = request.request_id.clone();
        let stream = self
            .closing
            .run_until_cancelled(llm.generate(request.into()))
            .await
            .ok_or(BackendError::Unavailable)?
            .map_err(BackendError::from_llm)?;
        let completion = stream.completion();
        Ok(BackendGeneration {
            stream: tracked_stream(
                stream,
                request_id,
                started_at,
                self.boundary_latency.clone(),
            ),
            completion: Box::pin(async move {
                let _source_read = source_read;
                completion
                    .wait()
                    .await
                    .map_err(BackendError::from_engine_client)
            }),
        })
    }

    async fn abort(&self, request_ids: &[String]) -> Result<(), BackendError> {
        let guard = self.llm.read().await;
        let llm = guard.as_ref().ok_or(BackendError::Unavailable)?;
        self.closing
            .run_until_cancelled(llm.abort(request_ids))
            .await
            .ok_or(BackendError::Unavailable)?
            .map_err(BackendError::from_llm)
    }

    async fn local_prefixes(
        &self,
        request: &foretoken_model_protocol::KvPrefixRequest,
    ) -> Result<Vec<foretoken_model_protocol::KvRankPrefix>, BackendError> {
        let guard = self.llm.read().await;
        let client = guard
            .as_ref()
            .ok_or(BackendError::Unavailable)?
            .engine_core_client();
        // Each scheduler reports its own rank; cache contents are not a consensus value.
        let observations = self
            .closing
            .run_until_cancelled(
                client.call_utility::<serde_json::Value, _>(
                    "get_prefix_cache_hit",
                    (
                        &request.prompt_token_ids,
                        request.cache_salt.as_deref(),
                        request.lora_name.as_deref(),
                        request
                            .multimodal
                            .iter()
                            .map(|feature| {
                                serde_json::json!({
                                    "identifier": feature.identifier,
                                    "offset": feature.offset,
                                    "length": feature.length,
                                })
                            })
                            .collect::<Vec<_>>(),
                        request.lora_path.as_deref(),
                    ),
                ),
            )
            .await
            .ok_or(BackendError::Unavailable)?
            .map_err(BackendError::from_engine_client)?;
        // Native dictionaries encode tiers as strings; rmpv's enum decoder expects arrays.
        observations
            .into_iter()
            .map(|value| serde_json::from_value(value).map_err(|_| BackendError::Protocol))
            .collect()
    }

    async fn retained_prefixes(
        &self,
        request: &foretoken_model_protocol::KvPrefixRequest,
    ) -> Result<Vec<foretoken_model_protocol::KvRetainedRankPrefix>, BackendError> {
        if !self.bidirectional_kv_xfer
            || request.lora_name.is_some()
            || !request.multimodal.is_empty()
        {
            return Ok(Vec::new());
        }
        self.kv_utility(
            "retained_prefix",
            serde_json::json!({
                "prompt_token_ids": request.prompt_token_ids,
                "cache_salt": request.cache_salt,
            }),
        )
        .await
    }

    async fn claim_kv_prefix(
        &self,
        request: &foretoken_model_protocol::KvPrefixClaim,
    ) -> Result<foretoken_model_protocol::KvPrefixClaimResult, BackendError> {
        use foretoken_model_protocol::KvPrefixClaimResult;
        if !self.bidirectional_kv_xfer
            || request.prefix.lora_name.is_some()
            || !request.prefix.multimodal.is_empty()
        {
            return Ok(KvPrefixClaimResult::Miss);
        }
        #[derive(serde::Deserialize)]
        struct Reply {
            dp_rank: u32,
            instance_id: String,
            #[serde(flatten)]
            result: KvPrefixClaimResult,
        }
        let results: Vec<Reply> = self
            .kv_utility(
                "claim",
                serde_json::json!({
                    "dp_rank": request.source.dp_rank,
                    "instance_id": request.source.instance_id,
                    "offer_id": request.source.offer_id,
                    "consumer_id": request.consumer_id,
                    "prompt_token_ids": request.prefix.prompt_token_ids,
                    "cache_salt": request.prefix.cache_salt,
                }),
            )
            .await?;
        // 重启或 rank 变化使旧来源失效；该请求继续普通 Prefill。
        Ok(results
            .into_iter()
            .find(|result| {
                result.dp_rank == request.source.dp_rank
                    && result.instance_id == request.source.instance_id
            })
            .map_or(KvPrefixClaimResult::Miss, |result| result.result))
    }

    async fn cancel_kv_prefix(
        &self,
        request: &foretoken_model_protocol::KvPrefixCancel,
    ) -> Result<foretoken_model_protocol::KvPrefixCancelResult, BackendError> {
        use foretoken_model_protocol::KvPrefixCancelResult;
        if !self.bidirectional_kv_xfer {
            return Ok(KvPrefixCancelResult::Miss);
        }
        #[derive(serde::Deserialize)]
        struct Reply {
            dp_rank: u32,
            instance_id: String,
            #[serde(flatten)]
            result: KvPrefixCancelResult,
        }
        let results: Vec<Reply> = self
            .kv_utility(
                "cancel",
                serde_json::json!({
                    "dp_rank": request.source.dp_rank,
                    "instance_id": request.source.instance_id,
                    "offer_id": request.source.offer_id,
                    "consumer_id": request.consumer_id,
                }),
            )
            .await?;
        Ok(results
            .into_iter()
            .find(|result| {
                result.dp_rank == request.source.dp_rank
                    && result.instance_id == request.source.instance_id
            })
            .map_or(KvPrefixCancelResult::Miss, |result| result.result))
    }

    async fn drain_kv_transfers(&self) -> Result<u64, BackendError> {
        if !self.kv_connector_utility {
            return Ok(0);
        }
        #[derive(serde::Deserialize)]
        struct Reply {
            active_leases: u64,
        }
        let results: Vec<Reply> = self.kv_utility("drain", serde_json::json!({})).await?;
        Ok(results.into_iter().map(|result| result.active_leases).sum())
    }

    fn telemetry(&self) -> BackendTelemetry {
        let vllm = read_vllm_metrics(&self.engine_labels);
        let (ttft_seconds, tpot_seconds, e2e_seconds) = self
            .boundary_latency
            .lock()
            .expect("boundary latency metrics lock poisoned")
            .snapshot();

        BackendTelemetry {
            data_parallel_ranks: vllm.data_parallel_ranks,
            request_cost: vllm.request_cost,
            max_concurrent_requests: self.max_concurrent_requests,
            scheduler_running_requests: vllm.scheduler_running_requests,
            scheduler_waiting_requests: vllm.scheduler_waiting_requests,
            kv_cache_usage: vllm.kv_cache_usage,
            prompt_tokens_total: vllm.prompt_tokens_total,
            generation_tokens_total: vllm.generation_tokens_total,
            ttft_seconds,
            tpot_seconds,
            e2e_seconds,
        }
    }

    fn render_openmetrics(&self) -> Result<String, MetricsError> {
        METRICS.render().map_err(|_| MetricsError)
    }

    async fn scheduler_load(&self) -> Result<Vec<SchedulerLoad>, BackendError> {
        let guard = self.llm.read().await;
        let llm = guard.as_ref().ok_or(BackendError::Unavailable)?;
        let loads = self
            .closing
            .run_until_cancelled(
                llm.engine_core_client()
                    .call_utility::<Option<SchedulerLoad>, _>("get_scheduler_load", ()),
            )
            .await
            .ok_or(BackendError::Unavailable)?
            .map_err(BackendError::from_engine_client)?;
        loads
            .into_iter()
            .collect::<Option<Vec<_>>>()
            .ok_or(BackendError::Unavailable)
    }
}

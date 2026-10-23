// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Adapts vLLM's public Llm facade, completion ownership, and metrics to the model-server backend.

pub mod config;
pub mod kv_events;
pub mod launch;
pub mod prefix_lookup;
mod profiling;
pub mod startup;
mod telemetry;
#[doc(hidden)]
pub mod transport;

use std::sync::{Arc, Mutex};
use std::time::Instant;

use async_trait::async_trait;
use foretoken_model_protocol::SchedulerLoad;
use futures::{Stream, StreamExt};
use tokio::sync::{RwLock, mpsc};
use tokio_util::sync::CancellationToken;
use vllm_engine_core_client::protocol::structured_outputs::StructuredOutputBackend;
use vllm_llm::{FinishReason, Llm};
use vllm_metrics::{EngineLabels, METRICS};

use self::kv_events::KvEventAdapter;
use self::prefix_lookup::SharedKvLookup;
use self::telemetry::{BoundaryLatencyMetrics, read_vllm_metrics};
use super::{
    Backend, BackendError, BackendGeneration, BackendTelemetry, ExecutionControl, GenerateInput,
    KvDeltaError, TokenErrorCode, TokenEvent, TokenStream,
};

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
    kv_events: Option<Arc<KvEventAdapter>>,
    shared_kv: Option<SharedKvLookup>,
    kv_source_endpoint: Option<String>,
}

impl VllmBackend {
    /// Retains the native facade, KV observations and physical source endpoint for the model-server lifetime.
    pub fn new(
        llm: Llm,
        max_concurrent_requests: Option<u64>,
        kv_events: Option<Arc<KvEventAdapter>>,
        shared_kv: Option<SharedKvLookup>,
        bidirectional_kv_xfer: bool,
        kv_connector_utility: bool,
        kv_source_endpoint: Option<String>,
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
            kv_events,
            shared_kv,
            kv_source_endpoint,
        }
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

    /// Waits for remote claim cleanup after executions drain, including after local engine exit.
    pub async fn wait_for_kv_cleanup(&self) {
        self.kv_cleanup.close();
        self.kv_cleanup.wait().await;
    }

    /// Counts unconfirmed remote claims for the supervisor's drain-timeout diagnostics.
    pub fn pending_kv_cleanup(&self) -> usize {
        self.kv_cleanup.len()
    }

    /// Calls the active connector through EngineCore and decodes each rank's utility response.
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
        // A stale reuse hint must not prevent ordinary Prefill on a replacement instance.
        let source_read = if let Some(source) = request
            .kv_reuse
            .take()
            .filter(|_| self.bidirectional_kv_xfer)
        {
            let mut read = super::kv_reuse::RemoteKvRead::new(
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
    ) -> Result<foretoken_model_protocol::KvLocalPrefixResponse, BackendError> {
        let adapter = self.kv_events.as_ref().ok_or(BackendError::Unavailable)?;
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
        let ranks = observations
            .into_iter()
            .map(|value| serde_json::from_value(value).map_err(|_| BackendError::Protocol))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(adapter.prefix_response(ranks))
    }

    async fn shared_prefix(
        &self,
        request: &foretoken_model_protocol::KvPrefixRequest,
    ) -> Result<foretoken_model_protocol::KvSharedPrefixResponse, BackendError> {
        self.shared_kv
            .as_ref()
            .ok_or(BackendError::Unavailable)?
            .lookup(request)
            .await
            .ok_or(BackendError::Unavailable)
    }

    fn kv_delta(
        &self,
        dp_rank: u32,
        epoch: Option<&str>,
        after: Option<u64>,
        limit: usize,
    ) -> Result<foretoken_model_protocol::KvDeltaResponse, KvDeltaError> {
        self.kv_events
            .as_ref()
            .ok_or(KvDeltaError::Unavailable)?
            .delta(dp_rank, epoch, after, limit)
    }

    async fn retained_prefixes(
        &self,
        request: &foretoken_model_protocol::KvPrefixRequest,
    ) -> Result<foretoken_model_protocol::KvRetainedPrefixResponse, BackendError> {
        let adapter = self.kv_events.as_ref().ok_or(BackendError::Unavailable)?;
        let endpoint = self
            .kv_source_endpoint
            .as_ref()
            .ok_or(BackendError::Unavailable)?;
        let ranks = if !self.bidirectional_kv_xfer
            || request.lora_name.is_some()
            || !request.multimodal.is_empty()
        {
            Vec::new()
        } else {
            self.kv_utility(
                "retained_prefix",
                serde_json::json!({
                    "prompt_token_ids": request.prompt_token_ids,
                    "cache_salt": request.cache_salt,
                }),
            )
            .await?
        };
        Ok(foretoken_model_protocol::KvRetainedPrefixResponse {
            model_group_id: adapter.model_group_id.clone(),
            scope_id: adapter.scope_id.clone(),
            model_server_endpoint: endpoint.clone(),
            ranks,
        })
    }

    async fn claim_kv_prefix(
        &self,
        request: &foretoken_model_protocol::KvPrefixClaim,
    ) -> Result<foretoken_model_protocol::KvPrefixClaimResult, BackendError> {
        use foretoken_model_protocol::KvPrefixClaimResult;
        if !self.bidirectional_kv_xfer
            || !self
                .kv_events
                .as_ref()
                .is_some_and(|source| source.model_group_id == request.source.route_target_id)
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
        // A replaced instance or DP rank invalidates the hint and falls back to ordinary Prefill.
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
        if !self.bidirectional_kv_xfer
            || !self
                .kv_events
                .as_ref()
                .is_some_and(|source| source.model_group_id == request.source.route_target_id)
        {
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

    fn encode_metrics(&self, output: &mut String) -> Result<(), std::fmt::Error> {
        prometheus_client::encoding::text::encode_registry(output, METRICS.registry())
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

impl BackendError {
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

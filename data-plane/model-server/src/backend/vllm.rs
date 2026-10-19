// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Adapts vLLM's public Llm facade, completion ownership, and metrics to the model-server backend.

mod telemetry;

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

use self::telemetry::{BoundaryLatencyMetrics, read_vllm_metrics};
use super::{
    Backend, BackendError, BackendGeneration, BackendTelemetry, GenerateInput, MetricsError,
    TokenErrorCode, TokenEvent, TokenStream,
};

/// vLLM adapter that retains its public `Llm` facade rather than its wire protocol.
pub struct VllmBackend {
    llm: RwLock<Option<Llm>>,
    closing: CancellationToken,
    max_concurrent_requests: Option<u64>,
    engine_labels: Vec<EngineLabels>,
    boundary_latency: Arc<Mutex<BoundaryLatencyMetrics>>,
}

impl VllmBackend {
    /// Creates the model-server adapter and retains the provided vLLM `Llm` facade until shutdown.
    pub fn new(llm: Llm, max_concurrent_requests: Option<u64>) -> Self {
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

#[async_trait]
impl Backend for VllmBackend {
    async fn generate(
        &self,
        mut request: GenerateInput,
    ) -> Result<BackendGeneration, BackendError> {
        // HTTP decoding discards backend hints; headless EngineCore skips frontend
        // validation and retains its first grammar backend. Use Xgrammar consistently
        // so JSON and structural-tag requests work regardless of arrival order.
        if let Some(outputs) = &mut request.sampling_params.structured_outputs {
            outputs.backend = StructuredOutputBackend::Xgrammar;
        }
        let started_at = Instant::now();
        let guard = self.llm.read().await;
        let llm = guard.as_ref().ok_or(BackendError::Unavailable)?;
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

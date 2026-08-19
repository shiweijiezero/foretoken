// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM adapter that retains its public `Llm` facade rather than its wire protocol.

use async_trait::async_trait;
use futures::{Stream, StreamExt};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use tokio::sync::{RwLock, mpsc};
use vllm_llm::{FinishReason, Llm};
use vllm_metrics::EngineLabels;

use super::conversion::to_token_output;
use super::telemetry::{BoundaryLatencyMetrics, read_vllm_metrics};
use crate::engine::{Engine, EngineCapabilities, EngineError, EngineTelemetry, TokenStream};
use foretoken_model_protocol::{GenerateInput, TokenErrorCode, TokenEvent};

/// vLLM adapter failures. Classified without retaining vLLM's diagnostic text,
/// then translated into the engine-neutral [`EngineError`] at the trait
/// boundary.
#[derive(Debug, thiserror::Error, Clone, Copy, PartialEq, Eq)]
pub enum VllmError {
    #[error("request was rejected")]
    Rejected,
    #[error("request is invalid")]
    InvalidRequest,
    #[error("vLLM is unavailable")]
    Unavailable,
    #[error("vLLM protocol failed")]
    Protocol,
    #[error("vLLM request failed")]
    RequestFailed,
}

impl VllmError {
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
            | Error::UnsupportedField { .. }
            | Error::ValueDecode(_) => Self::Protocol,
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

impl From<VllmError> for EngineError {
    fn from(error: VllmError) -> Self {
        match error {
            VllmError::Rejected => EngineError::Rejected,
            VllmError::InvalidRequest => EngineError::InvalidRequest,
            VllmError::Unavailable => EngineError::Unavailable,
            VllmError::Protocol => EngineError::Protocol,
            VllmError::RequestFailed => EngineError::RequestFailed,
        }
    }
}

/// vLLM adapter that retains its public `Llm` facade rather than its wire protocol.
pub struct VllmBackend {
    llm: RwLock<Option<Llm>>,
    running_requests: Arc<AtomicU64>,
    max_concurrent_requests: u64,
    engine_labels: Vec<EngineLabels>,
    boundary_latency: Arc<Mutex<BoundaryLatencyMetrics>>,
}

impl VllmBackend {
    pub fn new(llm: Llm, max_concurrent_requests: u64) -> Self {
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
            running_requests: Arc::new(AtomicU64::new(0)),
            max_concurrent_requests,
            engine_labels,
            boundary_latency: Arc::new(Mutex::new(BoundaryLatencyMetrics::new())),
        }
    }
}

struct InflightGuard {
    running_requests: Arc<AtomicU64>,
    released: AtomicBool,
}

impl InflightGuard {
    fn accepted(running_requests: Arc<AtomicU64>) -> Arc<Self> {
        running_requests.fetch_add(1, Ordering::AcqRel);
        Arc::new(Self {
            running_requests,
            released: AtomicBool::new(false),
        })
    }

    fn release(&self) {
        if !self.released.swap(true, Ordering::AcqRel) {
            self.running_requests.fetch_sub(1, Ordering::AcqRel);
        }
    }
}

impl Drop for InflightGuard {
    fn drop(&mut self) {
        self.release();
    }
}

/// Restore external request identity and track engine-boundary latency without consumer delay.
fn tracked_stream<S>(
    stream: S,
    request_id: String,
    started_at: Instant,
    running_requests: Arc<AtomicU64>,
    boundary_latency: Arc<Mutex<BoundaryLatencyMetrics>>,
) -> TokenStream
where
    S: Stream<Item = Result<vllm_llm::GenerateOutput, vllm_llm::Error>> + Send + 'static,
{
    let inflight = InflightGuard::accepted(running_requests);
    // Buffer one output while keeping response memory bounded. Once the producer blocks on that
    // buffer, later latency samples are omitted instead of including response-consumer delay.
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
                inflight.release();
                return;
            };
            let (event, terminal) = match item {
                Ok(output) if output.finish_reason == Some(FinishReason::Error) => {
                    inflight.release();
                    (
                        Ok(TokenEvent::Error {
                            request_id: request_id.clone(),
                            code: TokenErrorCode::RequestFailed,
                        }),
                        true,
                    )
                }
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
                    if terminal {
                        inflight.release();
                    }
                    output.request_id.clone_from(&request_id);
                    (
                        Ok(TokenEvent::Token(Box::new(to_token_output(output)))),
                        terminal,
                    )
                }
                Err(error) => {
                    inflight.release();
                    (Err(VllmError::from_llm(error).into()), true)
                }
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
impl Engine for VllmBackend {
    async fn generate(&self, request: GenerateInput) -> Result<TokenStream, EngineError> {
        let started_at = Instant::now();
        let guard = self.llm.read().await;
        let llm = guard.as_ref().ok_or(EngineError::Unavailable)?;
        let request_id = request.request_id.clone();
        let stream = llm
            .generate(super::conversion::to_vllm_request(request)?)
            .await
            .map_err(VllmError::from_llm)
            .map_err(EngineError::from)?;
        Ok(tracked_stream(
            stream,
            request_id,
            started_at,
            self.running_requests.clone(),
            self.boundary_latency.clone(),
        ))
    }

    async fn abort(&self, request_ids: &[String]) -> Result<(), EngineError> {
        let guard = self.llm.read().await;
        let llm = guard.as_ref().ok_or(EngineError::Unavailable)?;
        llm.abort(request_ids)
            .await
            .map_err(VllmError::from_llm)
            .map_err(EngineError::from)
    }

    fn telemetry(&self) -> EngineTelemetry {
        let vllm = read_vllm_metrics(&self.engine_labels);
        let (ttft_seconds, tpot_seconds, e2e_seconds) = self
            .boundary_latency
            .lock()
            .expect("boundary latency metrics lock poisoned")
            .snapshot();

        EngineTelemetry {
            running_requests: self.running_requests.load(Ordering::Acquire),
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

    fn capabilities(&self) -> EngineCapabilities {
        EngineCapabilities::default()
    }

    async fn cleanup(&self) -> Result<(), EngineError> {
        // `take` makes cleanup idempotent and null-safe against a partially
        // initialized backend.
        let Some(llm) = self.llm.write().await.take() else {
            return Ok(());
        };
        llm.shutdown()
            .await
            .map_err(VllmError::from_llm)
            .map_err(EngineError::from)
    }
}

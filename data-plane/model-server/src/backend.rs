// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine-neutral HTTP-facing boundary and the thin vLLM EngineCore adapter.

use async_trait::async_trait;
use futures::{Stream, StreamExt};
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use thiserror::Error;
use tokio::sync::RwLock;
use vllm_llm::{FinishReason, Llm};

pub use foretoken_model_protocol::{GenerateInput, TokenErrorCode, TokenEvent, TokenOutput};

/// Stream shape shared by production vLLM and deterministic test backends.
pub type TokenStream = Pin<Box<dyn Stream<Item = Result<TokenEvent, BackendError>> + Send>>;

/// Capacity visible to the group-local frontend. This contains no request or engine identity.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BackendTelemetry {
    pub running_requests: u64,
    pub max_concurrent_requests: u64,
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

impl BackendError {
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

/// Minimal EngineCore operations that this group-local server needs.
#[async_trait]
pub trait Backend: Send + Sync {
    async fn generate(&self, request: GenerateInput) -> Result<TokenStream, BackendError>;
    async fn abort(&self, request_ids: &[String]) -> Result<(), BackendError>;
    fn telemetry(&self) -> BackendTelemetry;
}

/// vLLM adapter that retains its public `Llm` facade rather than its wire protocol.
pub struct VllmBackend {
    llm: RwLock<Option<Llm>>,
    running_requests: Arc<AtomicU64>,
    max_concurrent_requests: u64,
}

impl VllmBackend {
    pub fn new(llm: Llm, max_concurrent_requests: u64) -> Self {
        Self {
            llm: RwLock::new(Some(llm)),
            running_requests: Arc::new(AtomicU64::new(0)),
            max_concurrent_requests,
        }
    }

    /// Stop local client tasks without duplicating the EngineCore protocol.
    pub async fn shutdown(&self) -> Result<(), BackendError> {
        let Some(llm) = self.llm.write().await.take() else {
            return Ok(());
        };
        llm.shutdown().await.map_err(BackendError::from_llm)
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

fn tracked_stream<S>(stream: S, running_requests: Arc<AtomicU64>) -> TokenStream
where
    S: Stream<Item = Result<vllm_llm::GenerateOutput, vllm_llm::Error>> + Send + 'static,
{
    let inflight = InflightGuard::accepted(running_requests);
    Box::pin(async_stream::stream! {
        let _inflight = inflight;
        let mut stream = Box::pin(stream);
        while let Some(item) = stream.next().await {
            match item {
                Ok(output) if output.finish_reason == Some(FinishReason::Error) => {
                    _inflight.release();
                    yield Ok(TokenEvent::Error { request_id: output.request_id, code: TokenErrorCode::RequestFailed });
                    return;
                }
                Ok(output) => {
                    let terminal = output.finish_reason.is_some();
                    if terminal { _inflight.release(); }
                    yield Ok(TokenEvent::Token(Box::new(output.into())));
                    if terminal { return; }
                }
                Err(error) => {
                    _inflight.release();
                    yield Err(BackendError::from_llm(error));
                    return;
                }
            }
        }
        _inflight.release();
    })
}

#[async_trait]
impl Backend for VllmBackend {
    async fn generate(&self, request: GenerateInput) -> Result<TokenStream, BackendError> {
        let guard = self.llm.read().await;
        let llm = guard.as_ref().ok_or(BackendError::Unavailable)?;
        let stream = llm
            .generate(request.into())
            .await
            .map_err(BackendError::from_llm)?;
        Ok(tracked_stream(stream, self.running_requests.clone()))
    }
    async fn abort(&self, request_ids: &[String]) -> Result<(), BackendError> {
        let guard = self.llm.read().await;
        let llm = guard.as_ref().ok_or(BackendError::Unavailable)?;
        llm.abort(request_ids).await.map_err(BackendError::from_llm)
    }
    fn telemetry(&self) -> BackendTelemetry {
        BackendTelemetry {
            running_requests: self.running_requests.load(Ordering::Acquire),
            max_concurrent_requests: self.max_concurrent_requests,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures::stream;
    use std::sync::Arc;
    use vllm_llm::{GenerateOutput, GeneratePromptInfo};

    fn output(finish_reason: Option<FinishReason>) -> GenerateOutput {
        GenerateOutput {
            request_id: "request".into(),
            prompt_info: Some(GeneratePromptInfo {
                prompt_token_ids: Arc::from([1]),
                prompt_logprobs: None,
            }),
            token_ids: vec![1],
            logprobs: None,
            finish_reason,
            cached_token_count: 0,
            kv_transfer_params: None,
            ec_transfer_params: None,
        }
    }

    #[tokio::test]
    async fn stream_guard_releases_on_terminal_error_and_drop() {
        let running = Arc::new(AtomicU64::new(0));
        let mut terminal = tracked_stream(
            stream::iter([Ok(output(Some(FinishReason::stop_eos())))]),
            running.clone(),
        );
        assert_eq!(running.load(Ordering::Acquire), 1);
        assert!(terminal.next().await.unwrap().is_ok());
        assert_eq!(running.load(Ordering::Acquire), 0);

        let mut error = tracked_stream(
            stream::iter([Err(vllm_llm::Error::EngineCoreClient(
                vllm_engine_core_client::Error::RequestStreamClosed {
                    request_id: "request".into(),
                },
            ))]),
            running.clone(),
        );
        assert!(error.next().await.unwrap().is_err());
        assert_eq!(running.load(Ordering::Acquire), 0);

        let mut dropped = tracked_stream(stream::iter([Ok(output(None))]), running.clone());
        assert!(dropped.next().await.unwrap().is_ok());
        assert_eq!(running.load(Ordering::Acquire), 1);
        drop(dropped);
        assert_eq!(running.load(Ordering::Acquire), 0);
    }
}

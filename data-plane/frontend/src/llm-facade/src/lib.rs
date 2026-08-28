// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Backend-neutral generation execution port and vLLM adapter.
mod facade;
mod http;
use async_trait::async_trait;
pub use facade::{
    consume_encoder, consume_prefill, encoder_stage_request, inject_ec_transfer_params,
    pd_stage_requests, reject_client_transfer_params,
};
use foretoken_router::RouteDecision;
use futures::Stream;
pub use http::{HttpFacade, bootstrap_engine_id};
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use thiserror::Error;
use vllm_llm::{GenerateOutput, GenerateRequest};
pub type TokenStream = Pin<Box<dyn Stream<Item = Result<GenerateOutput, LlmFacadeError>> + Send>>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RouteStage {
    Aggregate,
    Encoder,
    Prefill,
    Decode,
}
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum LlmFacadeError {
    #[error("backend unavailable")]
    Unavailable,
    #[error("backend rejected request")]
    Rejected,
    #[error("backend protocol failed")]
    Protocol,
    #[error("backend request failed")]
    RequestFailed,
    #[error("backend configuration is invalid")]
    Configuration,
}
#[async_trait]
pub trait LlmFacade: Send + Sync {
    /// Starts one backend generation for the execution workflow.
    ///
    /// Returns a live token stream owned by the caller until terminal output or drop; failures
    /// before stream creation describe a request that never entered streaming.
    async fn generate(&self, request: GenerateRequest) -> Result<TokenStream, LlmFacadeError>;

    /// Requests cleanup of backend work whose consuming workflow has ended early.
    ///
    /// Drop guards call this best-effort operation for admitted request IDs. Success only confirms
    /// that the abort request was accepted; the backend owns final request termination.
    async fn abort(&self, request_ids: &[String]) -> Result<(), LlmFacadeError>;
}

struct AbortOnDrop {
    facade: Option<Arc<dyn LlmFacade>>,
    request_id: Option<String>,
    runtime: Option<tokio::runtime::Handle>,
}

impl AbortOnDrop {
    fn disarm(&mut self) {
        self.facade = None;
        self.request_id = None;
    }

    fn spawn_abort(&mut self) {
        let (Some(facade), Some(request_id)) = (self.facade.take(), self.request_id.take()) else {
            return;
        };
        let runtime = self
            .runtime
            .clone()
            .or_else(|| tokio::runtime::Handle::try_current().ok());
        if let Some(runtime) = runtime {
            runtime.spawn(async move {
                let _ = facade.abort(&[request_id]).await;
            });
        }
    }
}

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.spawn_abort();
    }
}

struct AbortableStream {
    stream: TokenStream,
    abort: AbortOnDrop,
}

impl Stream for AbortableStream {
    type Item = Result<GenerateOutput, LlmFacadeError>;

    fn poll_next(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        match self.stream.as_mut().poll_next(context) {
            Poll::Ready(Some(Ok(output))) => {
                if output.finish_reason.is_some() {
                    self.abort.disarm();
                }
                Poll::Ready(Some(Ok(output)))
            }
            Poll::Ready(Some(Err(error))) => {
                self.abort.spawn_abort();
                Poll::Ready(Some(Err(error)))
            }
            Poll::Ready(None) => {
                self.abort.spawn_abort();
                Poll::Ready(None)
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

/// Aborts backend work when its output stream is dropped before a terminal output.
pub fn abort_on_drop(
    facade: Arc<dyn LlmFacade>,
    request_id: String,
    stream: TokenStream,
) -> TokenStream {
    Box::pin(AbortableStream {
        stream,
        abort: AbortOnDrop {
            facade: Some(facade),
            request_id: Some(request_id),
            runtime: tokio::runtime::Handle::try_current().ok(),
        },
    })
}

/// Best-effort cleanup for execution stages that have been admitted but not completed.
///
/// Register a stage before its `generate` future is awaited.  Dropping the guard aborts every
/// registered child request; handing it to `with_stream` keeps that ownership until a terminal
/// output makes the entire workflow successful.
pub struct MultiStageCleanup {
    stages: Vec<(Arc<dyn LlmFacade>, String)>,
    runtime: Option<tokio::runtime::Handle>,
}

impl Default for MultiStageCleanup {
    fn default() -> Self {
        Self::new()
    }
}

impl MultiStageCleanup {
    /// Starts cleanup ownership for workflow stages that may outlive a failed handoff.
    ///
    /// The E/P/D workflow registers child requests on the returned guard, which aborts them when
    /// dropped unless terminal output transfers or disarms that ownership.
    pub fn new() -> Self {
        Self {
            stages: Vec::new(),
            runtime: tokio::runtime::Handle::try_current().ok(),
        }
    }

    /// Adds an admitted child request to this workflow's cleanup set.
    ///
    /// Stage executors call this before awaiting generation so a later failure aborts the request;
    /// it produces no handle because this guard retains ownership until completion.
    pub fn register(&mut self, facade: Arc<dyn LlmFacade>, request_id: String) {
        self.stages.push((facade, request_id));
    }

    /// Transfers cleanup ownership to the final backend stream returned to HTTP response handling.
    ///
    /// Returns a wrapped stream that retains registered stages until terminal output, then disarms
    /// them; an early drop or stream failure aborts every remaining stage.
    pub fn with_stream(self, stream: TokenStream) -> TokenStream {
        Box::pin(CleanupStream {
            stream,
            cleanup: self,
        })
    }

    fn disarm(&mut self) {
        self.stages.clear();
    }

    fn spawn_abort(&mut self) {
        if self.stages.is_empty() {
            return;
        }
        let stages = std::mem::take(&mut self.stages);
        let runtime = self
            .runtime
            .clone()
            .or_else(|| tokio::runtime::Handle::try_current().ok());
        if let Some(runtime) = runtime {
            runtime.spawn(async move {
                for (facade, request_id) in stages {
                    let _ = facade.abort(&[request_id]).await;
                }
            });
        }
    }
}

impl Drop for MultiStageCleanup {
    fn drop(&mut self) {
        self.spawn_abort();
    }
}

struct CleanupStream {
    stream: TokenStream,
    cleanup: MultiStageCleanup,
}

impl Stream for CleanupStream {
    type Item = Result<GenerateOutput, LlmFacadeError>;

    fn poll_next(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        match self.stream.as_mut().poll_next(context) {
            Poll::Ready(Some(Ok(output))) => {
                if output.finish_reason.is_some() {
                    self.cleanup.disarm();
                }
                Poll::Ready(Some(Ok(output)))
            }
            Poll::Ready(Some(Err(error))) => {
                self.cleanup.spawn_abort();
                Poll::Ready(Some(Err(error)))
            }
            Poll::Ready(None) => {
                self.cleanup.spawn_abort();
                Poll::Ready(None)
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

/// Resolves selected execution stages without exposing registry ownership.
pub trait LlmFacadeResolver: Send + Sync {
    /// Resolves exactly one selected stage. Router Core keeps all health,
    /// capacity, topology, and pipeline-scope decisions; execution only opens endpoints.
    fn resolve_stage(
        &self,
        decision: &RouteDecision,
        stage: RouteStage,
    ) -> Option<Arc<dyn LlmFacade>>;

    /// Returns the live prefill bootstrap endpoint for P/D request construction.
    ///
    /// The workflow reads it immediately before starting prefill and passes it to the decode
    /// connector; `None` prevents the workflow from admitting that staged request.
    fn bootstrap_endpoint(&self, prefill: &RouteDecision) -> Option<String>;
}

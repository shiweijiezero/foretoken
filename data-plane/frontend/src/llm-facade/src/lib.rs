// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//! Backend-neutral generation execution port and vLLM adapter.
mod facade;
mod http;
use async_trait::async_trait;
pub use facade::VllmFacade;
use foretoken_router::ExecutionPlan;
use futures::Stream;
pub use http::{HttpFacade, bootstrap_engine_id};
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use thiserror::Error;
use vllm_llm::{GenerateOutput, GenerateRequest};
pub type TokenStream = Pin<Box<dyn Stream<Item = Result<GenerateOutput, LlmFacadeError>> + Send>>;
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
impl LlmFacadeError {
    pub const fn retryable(self) -> bool {
        matches!(self, Self::Unavailable | Self::RequestFailed)
    }
    pub const fn affects_health(self) -> bool {
        matches!(self, Self::Unavailable | Self::Protocol)
    }
}
#[async_trait]
pub trait LlmFacade: Send + Sync {
    async fn generate(&self, request: GenerateRequest) -> Result<TokenStream, LlmFacadeError>;
    async fn abort(&self, request_ids: &[String]) -> Result<(), LlmFacadeError>;
}

struct AbortOnDrop {
    facade: Option<Arc<dyn LlmFacade>>,
    request_id: Option<String>,
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
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
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
        },
    })
}

/// Resolves an aggregate execution target or a dynamic P/D pair without exposing registry ownership.
pub trait LlmFacadeResolver: Send + Sync {
    fn resolve(&self, plan: &ExecutionPlan) -> Option<Arc<dyn LlmFacade>>;
}
#[cfg(test)]
#[path = "tests/facade.rs"]
mod tests;

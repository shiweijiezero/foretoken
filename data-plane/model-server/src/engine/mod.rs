// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine boundary between the model-server core and an inference engine
//! adapter.
//!
//! The trait uses vLLM's native types for the generate wire; the SGLang
//! adapter converts them to its own representation internally. The core relies
//! on this trait plus the engine-local error, telemetry, and capability types
//! below.

#[cfg(feature = "backend-sglang")]
pub mod sglang;
#[cfg(feature = "backend-vllm")]
pub mod vllm;

use std::pin::Pin;

use async_trait::async_trait;
use foretoken_model_protocol::{CumulativeHistogram, TokenErrorCode};
use futures::Stream;
use vllm_llm::{GenerateOutput, GenerateRequest};

/// Stream of per-request generate outputs produced by an engine.
pub type TokenStream = Pin<Box<dyn Stream<Item = Result<GenerateOutput, EngineError>> + Send>>;

/// Engine failures classified without retaining engine-specific diagnostic text.
#[derive(Debug, thiserror::Error, Clone, Copy, PartialEq, Eq)]
pub enum EngineError {
    #[error("request was rejected")]
    Rejected,
    #[error("request is invalid")]
    InvalidRequest,
    #[error("engine is unavailable")]
    Unavailable,
    #[error("engine protocol failed")]
    Protocol,
    #[error("engine request failed")]
    RequestFailed,
}

impl EngineError {
    pub const fn token_error_code(self) -> TokenErrorCode {
        match self {
            Self::Unavailable => TokenErrorCode::Unavailable,
            Self::Rejected | Self::InvalidRequest | Self::Protocol => TokenErrorCode::Protocol,
            Self::RequestFailed => TokenErrorCode::RequestFailed,
        }
    }
}

/// Cumulative engine observations included in a telemetry snapshot.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct EngineTelemetry {
    pub running_requests: u64,
    pub max_concurrent_requests: u64,
    pub scheduler_running_requests: Option<u64>,
    pub scheduler_waiting_requests: Option<u64>,
    pub kv_cache_usage: Option<f64>,
    pub prompt_tokens_total: Option<u64>,
    pub generation_tokens_total: Option<u64>,
    pub ttft_seconds: CumulativeHistogram,
    pub tpot_seconds: CumulativeHistogram,
    pub e2e_seconds: CumulativeHistogram,
}

/// Minimal engine operations the group-local model-server core needs.
#[async_trait]
pub trait Engine: Send + Sync {
    async fn generate(&self, request: GenerateRequest) -> Result<TokenStream, EngineError>;
    async fn abort(&self, request_ids: &[String]) -> Result<(), EngineError>;
    fn telemetry(&self) -> EngineTelemetry;
    /// Releases engine resources. Must be idempotent and null-safe: repeated
    /// calls, and calls after a partially completed startup, return without
    /// error.
    async fn cleanup(&self) -> Result<(), EngineError>;
}

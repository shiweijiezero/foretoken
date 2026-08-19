// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine-neutral contract between the model-server core and an inference
//! engine adapter.
//!
//! This module defines the trait, error, telemetry, and capability types the
//! core relies on. It never imports an inference-engine crate; vLLM, SGLang,
//! and future engines implement [`Engine`] behind this boundary.

pub mod vllm;

use std::pin::Pin;
use std::str::FromStr;

use async_trait::async_trait;
use foretoken_model_protocol::{CumulativeHistogram, GenerateInput, TokenErrorCode, TokenEvent};
use futures::Stream;
use serde::{Deserialize, Serialize};

/// Stream of per-request token events produced by an engine.
pub type TokenStream = Pin<Box<dyn Stream<Item = Result<TokenEvent, EngineError>> + Send>>;

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

/// Engine discriminator carried by the launch plan envelope.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EngineKind {
    #[default]
    Vllm,
}

impl EngineKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Vllm => "vllm",
        }
    }
}

impl FromStr for EngineKind {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "vllm" => Ok(Self::Vllm),
            other => Err(format!("unsupported engine kind {other:?}")),
        }
    }
}

/// Capabilities an engine advertises. Every field is optional or a fallible
/// boolean: routing degrades to neutral scoring for capabilities an engine
/// does not advertise.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EngineCapabilities {
    /// Maximum context length served by this engine, when known.
    pub context_length: Option<u32>,
    /// Block-structured KV cache block size, when the engine exposes one.
    pub kv_cache_block_size: Option<u32>,
    /// Total KV blocks available, when the engine exposes them.
    pub total_kv_blocks: Option<u64>,
    /// Maximum number of concurrent sequences, when known.
    pub max_num_seqs: Option<u32>,
    /// Whether the engine can publish KV lifecycle events for prefix scoring.
    pub kv_event_sources: bool,
    /// Whether the engine supports disaggregated prefill/decode.
    pub supports_pd: bool,
    /// Whether the engine supports disaggregated encoder serving.
    pub supports_ec: bool,
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
    async fn generate(&self, request: GenerateInput) -> Result<TokenStream, EngineError>;
    async fn abort(&self, request_ids: &[String]) -> Result<(), EngineError>;
    fn telemetry(&self) -> EngineTelemetry;
    fn capabilities(&self) -> EngineCapabilities;
    /// Drains accepted in-flight work before transport teardown. Engines with no
    /// extra drain step (beyond the core's admission close) keep the no-op
    /// default.
    async fn drain(&self) -> Result<(), EngineError> {
        Ok(())
    }
    /// Releases engine resources. Must be idempotent and null-safe: repeated
    /// calls, and calls after a partially completed startup, return without
    /// error.
    async fn cleanup(&self) -> Result<(), EngineError>;
}

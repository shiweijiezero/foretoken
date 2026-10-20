// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine-neutral operations, output ownership, and failures consumed by internal HTTP handlers.

pub mod vllm;
pub mod vllmomni;

use std::future::Future;
use std::pin::Pin;

use async_trait::async_trait;
use foretoken_model_protocol::SchedulerLoad;
use futures::Stream;
use thiserror::Error;

pub use foretoken_model_protocol::{
    CumulativeHistogram, GenerateInput, TokenErrorCode, TokenEvent, TokenOutput,
};
pub use vllm::VllmBackend;

/// Engine-neutral output stream consumed by internal HTTP handlers.
pub type TokenStream = Pin<Box<dyn Stream<Item = Result<TokenEvent, BackendError>> + Send>>;

/// Output delivery and engine-origin termination have separate lifetimes.
/// The API execution owner retains completion even after its HTTP consumer disconnects.
pub struct BackendGeneration {
    pub stream: TokenStream,
    pub completion: Pin<Box<dyn Future<Output = Result<(), BackendError>> + Send>>,
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

/// Backend failures classified without retaining inference-engine diagnostic text.
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

/// A KV reader either retries unavailable observations or replaces its cursor with the returned epoch.
#[derive(Debug)]
pub enum KvDeltaError {
    Unavailable,
    CursorReset(Box<foretoken_model_protocol::KvDeltaResponse>),
}

/// Minimal inference backend operations that this group-local server needs.
#[async_trait]
pub trait Backend: Send + Sync {
    /// Submits one request and returns separate output and engine-completion ownership to the API task.
    async fn generate(&self, request: GenerateInput) -> Result<BackendGeneration, BackendError>;

    /// Cancels request IDs supplied by the abort handler; backend ownership remains unchanged.
    async fn abort(&self, request_ids: &[String]) -> Result<(), BackendError>;

    /// Observes local scheduler prefixes and binds them to the backend's model/layout identity.
    /// HTTP callers receive owned observations without allocating request capacity.
    async fn local_prefixes(
        &self,
        request: &foretoken_model_protocol::KvPrefixRequest,
    ) -> Result<foretoken_model_protocol::KvLocalPrefixResponse, BackendError>;

    /// Reads the selected connector's shared prefix without retaining request or result state.
    async fn shared_prefix(
        &self,
        request: &foretoken_model_protocol::KvPrefixRequest,
    ) -> Result<foretoken_model_protocol::KvSharedPrefixResponse, BackendError>;

    /// Returns rank-local changes or a cursor reset while the backend retains event history.
    fn kv_delta(
        &self,
        dp_rank: u32,
        epoch: Option<&str>,
        after: Option<u64>,
        limit: usize,
    ) -> Result<foretoken_model_protocol::KvDeltaResponse, KvDeltaError>;

    /// Returns a snapshot published by telemetry handlers without transferring backend ownership.
    fn telemetry(&self) -> BackendTelemetry;

    /// Reads scheduler-owned token demand for the optional autoscaling observation endpoint.
    /// Failure leaves ordinary inference and telemetry available without inventing zero load.
    async fn scheduler_load(&self) -> Result<Vec<SchedulerLoad>, BackendError>;

    /// Appends engine metrics to the scrape body; the HTTP handler owns the document's EOF marker.
    fn encode_metrics(&self, output: &mut String) -> Result<(), std::fmt::Error>;
}

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
}

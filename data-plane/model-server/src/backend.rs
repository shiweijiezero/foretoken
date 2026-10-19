// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine-neutral operations, output ownership, and failures consumed by internal HTTP handlers.

mod vllm;

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use foretoken_model_protocol::SchedulerLoad;
use futures::Stream;
use thiserror::Error;
use tokio_util::sync::CancellationToken;

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

/// Serializes cancellation and native submission for one API-owned execution.
pub struct ExecutionControl {
    pub(crate) owner: String,
    phase: Mutex<ExecutionPhase>,
    cancellation: CancellationToken,
}

#[derive(Default)]
enum ExecutionPhase {
    #[default]
    Preparing,
    Submitted,
    Cancelled,
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

/// Backend-neutral failure to encode the active inference engine's metrics.
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
#[error("backend metrics rendering failed")]
pub struct MetricsError;

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

    /// Queries retained Decode prefixes across DP schedulers without acquiring their blocks.
    async fn retained_prefixes(
        &self,
        request: &foretoken_model_protocol::KvPrefixRequest,
    ) -> Result<Vec<foretoken_model_protocol::KvRetainedRankPrefix>, BackendError>;

    /// Claims a source prefix, returning Granted only after its native workers retain the blocks.
    async fn claim_kv_prefix(
        &self,
        request: &foretoken_model_protocol::KvPrefixClaim,
    ) -> Result<foretoken_model_protocol::KvPrefixClaimResult, BackendError>;

    /// Cancels an unstarted read; InUse leaves release to native transfer completion.
    async fn cancel_kv_prefix(
        &self,
        request: &foretoken_model_protocol::KvPrefixCancel,
    ) -> Result<foretoken_model_protocol::KvPrefixCancelResult, BackendError>;

    /// Closes source acquisition and reports pending native transfers to the shutdown supervisor.
    async fn drain_kv_transfers(&self) -> Result<u64, BackendError>;

    /// Returns a snapshot published by telemetry handlers without transferring backend ownership.
    fn telemetry(&self) -> BackendTelemetry;

    /// Reads scheduler-owned token demand for the optional autoscaling observation endpoint.
    /// Failure leaves ordinary inference and telemetry available without inventing zero load.
    async fn scheduler_load(&self) -> Result<Vec<SchedulerLoad>, BackendError>;

    /// Renders the OpenMetrics payload published by the metrics handler as an owned response body.
    fn render_openmetrics(&self) -> Result<String, MetricsError>;
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

impl ExecutionControl {
    /// Signals cancellation from abort or disconnect, preventing submission while preparing.
    pub fn cancel(&self) {
        let mut phase = self.phase.lock().expect("execution phase lock poisoned");
        if matches!(*phase, ExecutionPhase::Preparing) {
            *phase = ExecutionPhase::Cancelled;
        }
        self.cancellation.cancel();
    }

    /// Waits for cancellation; submitted work still requires engine-origin completion.
    pub async fn cancelled(&self) {
        self.cancellation.cancelled().await;
    }

    /// Reports cancellation to pre-submission waiters.
    pub fn is_cancelled(&self) -> bool {
        self.cancellation.is_cancelled()
    }

    /// Lets API cleanup distinguish unsubmitted work from an uncertain native execution.
    pub fn is_submitted(&self) -> bool {
        matches!(
            *self.phase.lock().expect("execution phase lock poisoned"),
            ExecutionPhase::Submitted
        )
    }

    /// Enters native submission atomically with respect to preparation cancellation.
    fn begin_submission(&self) -> bool {
        let mut phase = self.phase.lock().expect("execution phase lock poisoned");
        if !matches!(*phase, ExecutionPhase::Preparing) {
            return false;
        }
        *phase = ExecutionPhase::Submitted;
        true
    }
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

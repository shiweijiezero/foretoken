// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request admission and resource ownership before preprocessing and execution.

pub mod algorithm;
mod config;
mod context;
mod permit;
mod request;
mod telemetry;

use std::sync::Arc;
use thiserror::Error;

pub use config::{AdmissionConfig, AdmissionConfigError, AdmissionDescriptor};
pub use context::{
    AdmissionContext, AdmissionIdentity, AdmissionModelState, AdmissionModelStatus,
    AdmissionObjectives, AdmissionService, AdmissionStateReader, AdmissionTargetState,
};
pub use permit::{AdmissionPermit, AdmissionReservation};
pub use request::{
    AdmissionApi, AdmissionInput, AdmissionInputKind, AdmissionMedia, AdmissionOperation,
    AdmissionOutput, AdmissionRequest, AdmissionTokenCount,
};
pub use telemetry::{
    AdmissionAttempt, AdmissionCapacity, AdmissionQueueObservation, AdmissionQueueWait,
    mark_request_deadline, observe_http, render_metrics,
};

/// Configured admission rule and its process-local metric ownership.
/// The frontend shares this owner across HTTP requests and model-runtime updates.
pub struct Admission {
    rule: Arc<dyn AdmissionRule>,
    _metrics: telemetry::AdmissionMetricsScope,
}

impl Admission {
    /// Returns the configured rule for HTTP intake and runtime work admission.
    pub fn rule(&self) -> &dyn AdmissionRule {
        self.rule.as_ref()
    }
}

/// Accepts complete requests and owns any admission waiting and capacity reservations.
/// A successful result already holds its resources; dropping the future cancels its waiter.
#[async_trait::async_trait]
pub trait AdmissionRule: Send + Sync {
    /// Advertises the rule's finite resource limits for process-local observability.
    fn capacity(&self) -> Option<AdmissionCapacity> {
        None
    }

    /// Requires the runtime to check model preparation before admission, without waiting for it.
    fn requires_ready_runtime(&self) -> bool {
        false
    }

    /// Reserves one resident HTTP request before body extraction, without waiting.
    /// A resource-free permit leaves HTTP intake behavior unchanged.
    fn try_reserve_request(&self) -> Result<AdmissionPermit, AdmissionError> {
        Ok(AdmissionPermit::default())
    }

    /// Reserves the entire request weight atomically, waiting only within its original budget.
    async fn admit(
        &self,
        request: &AdmissionRequest,
        context: &AdmissionContext<'_>,
    ) -> Result<AdmissionPermit, AdmissionError>;

    /// Wakes admission waiters on shutdown without revoking permits held by running work.
    fn close(&self) {}
}

/// Admission outcomes translated by protocol adapters before response headers.
#[derive(Clone, Copy, Debug, Error, PartialEq, Eq)]
pub enum AdmissionError {
    #[error("generation service is overloaded")]
    Overloaded,
    #[error("admission queue timeout exceeded")]
    QueueTimeout,
    #[error("request deadline exceeded")]
    DeadlineExceeded,
    #[error("request fan-out exceeds configured admission concurrency")]
    BatchTooLarge,
    #[error("generation admission is closed")]
    Closed,
}

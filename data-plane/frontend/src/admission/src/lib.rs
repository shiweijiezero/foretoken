// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request admission and resource ownership before preprocessing and execution.

pub mod algorithm;
mod config;
mod context;
mod permit;
mod registry;
mod request;
mod telemetry;

use thiserror::Error;

pub use config::{AdmissionConfig, AdmissionConfigError, AdmissionDescriptor};
pub use context::{
    AdmissionContext, AdmissionIdentity, AdmissionModelState, AdmissionModelStatus,
    AdmissionObjectives, AdmissionService, AdmissionStateReader, AdmissionTargetState,
};
pub use permit::{AdmissionPermit, AdmissionReservation};
pub use registry::{Admission, AdmissionRegistry, PreparedAdmissions};
pub use request::{
    AdmissionApi, AdmissionInput, AdmissionInputKind, AdmissionMedia, AdmissionOperation,
    AdmissionOutput, AdmissionRequest, AdmissionTokenCount,
};
pub use telemetry::{
    AdmissionAttempt, AdmissionCapacity, AdmissionMetricsHandle, AdmissionQueueObservation,
    AdmissionQueueWait, mark_request_deadline, observe_http, render_metrics,
};

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

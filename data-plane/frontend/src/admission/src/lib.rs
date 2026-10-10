// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Role-based request waiting and fair dispatch before Router instance selection.

mod config;
mod permit;
mod registry;
mod request;
mod telemetry;

use thiserror::Error;

pub use config::{AdmissionConfig, AdmissionConfigError, CallerCapacity, RoleRule};
pub use foretoken_request_ledger::{RequestLedger, ReservationRef};
pub use permit::{AdmissionPermit, DispatchRules};
pub use registry::{Admission, AdmissionRegistry, PreparedAdmissions};
pub use request::{AdmissionRequest, CallerIdentity, with_caller};
pub use telemetry::{AdmissionAttempt, mark_request_deadline, observe_http, render_metrics};

/// Admission outcomes translated into each external protocol's existing error envelope.
#[derive(Clone, Copy, Debug, Error, PartialEq, Eq)]
pub enum AdmissionError {
    #[error("generation service is overloaded")]
    Overloaded,
    #[error("admission queue timeout exceeded")]
    QueueTimeout,
    #[error("request deadline exceeded")]
    DeadlineExceeded,
    #[error("request fan-out exceeds configured admission capacity")]
    BatchTooLarge,
    #[error("generation admission is closed")]
    Closed,
    #[error("configured role rules require a matching role and trusted caller identifier")]
    IdentityRequired,
    #[error("request capacity store is unavailable")]
    StoreUnavailable,
}

impl From<foretoken_request_ledger::LedgerError> for AdmissionError {
    fn from(error: foretoken_request_ledger::LedgerError) -> Self {
        if matches!(&error, foretoken_request_ledger::LedgerError::OwnerClosed) {
            return Self::Closed;
        }
        tracing::warn!(error = %error, "request capacity operation failed");
        Self::StoreUnavailable
    }
}

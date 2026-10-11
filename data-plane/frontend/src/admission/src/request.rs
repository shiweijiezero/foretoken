// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request identity and candidate weight, independent of external API dialects.

use std::future::Future;
use std::time::Instant;

/// Identity supplied by the trusted API entry point, not verified by the frontend.
#[derive(Clone, Debug, Default)]
pub struct CallerIdentity {
    pub caller: Option<String>,
    pub role: Option<String>,
}

tokio::task_local! {
    static CALLER: CallerIdentity;
}

/// Attaches ingress identity to one HTTP request while its protocol adapter runs.
/// Admission captures an owned copy before work moves into preparation or streaming tasks.
pub async fn with_caller<T>(identity: CallerIdentity, work: impl Future<Output = T>) -> T {
    CALLER.scope(identity, work).await
}

impl CallerIdentity {
    /// Captures the HTTP caller for admission; non-HTTP callers carry no inferred identity.
    pub fn current() -> Self {
        CALLER.try_with(Clone::clone).unwrap_or_default()
    }
}

/// Facts needed to reserve an entire request before preprocessing its candidates.
#[derive(Clone, Debug)]
pub struct AdmissionRequest {
    pub model: String,
    pub caller: CallerIdentity,
    /// Includes all generated candidates, including those used only for best-of selection.
    pub units: u32,
    pub received_at: Instant,
}

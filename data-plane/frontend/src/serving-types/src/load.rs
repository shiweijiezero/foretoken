// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Read-only frontend reservations, separate from backend telemetry.

/// Snapshot of this frontend's reservations for one route target and data-parallel rank.
/// Engine telemetry is deliberately not added to these values.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct RoutingLoadSnapshot {
    /// Requests reserved by a selected stage or response stream.
    pub requests: i64,
    /// Uncached prompt tokens reserved by this frontend.
    pub tokens: i64,
}

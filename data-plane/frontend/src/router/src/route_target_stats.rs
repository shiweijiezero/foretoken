// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Read-only route target statistics used by Router candidate snapshots.

use std::time::Duration;

use crate::{RouteTargetId, RouteTargetStats};

/// Reads locally cached route target statistics without request-path network I/O.
pub trait RouteTargetStatsReader: Send + Sync {
    /// Calculates statistics for `route_target_id` over the Router-selected `window`.
    ///
    /// Returns `None` when telemetry is unavailable. Gauges are available from the first snapshot;
    /// rates and latencies remain `None` until retained history covers the requested window.
    fn stats(&self, route_target_id: &RouteTargetId, window: Duration) -> Option<RouteTargetStats>;
}

/// Route Target-statistics reader used when telemetry is not configured.
pub(crate) struct NoopRouteTargetStatsReader;

impl RouteTargetStatsReader for NoopRouteTargetStatsReader {
    #[allow(unused_variables)]
    fn stats(&self, route_target_id: &RouteTargetId, window: Duration) -> Option<RouteTargetStats> {
        None
    }
}

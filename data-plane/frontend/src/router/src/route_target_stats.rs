// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Read-only route target statistics used by Router candidate snapshots.

use std::time::Duration;

use crate::RouteTargetId;

/// Latency distribution calculated over one observation window.
#[derive(Debug, Clone, PartialEq)]
pub struct RouteTargetLatencyStats {
    /// Samples observed in the window.
    pub samples: u64,
    /// Arithmetic mean in milliseconds.
    pub average_ms: f64,
    /// Bucket-derived p95, or `None` when it exceeds the largest exported boundary.
    pub p95_ms: Option<f64>,
}

/// Latest route target gauges and statistics calculated over `observed_window`.
#[derive(Debug, Clone, PartialEq)]
pub struct RouteTargetStats {
    /// Collection time of the latest cumulative snapshot.
    pub collected_at_unix_ms: u64,
    /// Actual interval between the Router-selected baseline and latest snapshot.
    pub observed_window: Duration,
    /// Requests currently admitted by Model Server.
    pub running_requests: u64,
    /// Configured Model Server concurrency limit.
    pub max_concurrent_requests: u64,
    /// Requests currently running in the vLLM scheduler.
    pub scheduler_running_requests: Option<u64>,
    /// Requests currently waiting in the vLLM scheduler.
    pub scheduler_waiting_requests: Option<u64>,
    /// Current vLLM KV-cache utilization in the range `0.0..=1.0`.
    pub kv_cache_usage: Option<f64>,
    /// Prompt tokens processed per second over `observed_window`.
    pub prompt_tokens_per_second: Option<f64>,
    /// Generated tokens per second over `observed_window`.
    pub generation_tokens_per_second: Option<f64>,
    /// Time to first token over `observed_window`.
    pub ttft: Option<RouteTargetLatencyStats>,
    /// Time per output token over `observed_window`.
    pub tpot: Option<RouteTargetLatencyStats>,
    /// End-to-end request latency over `observed_window`.
    pub e2e_latency: Option<RouteTargetLatencyStats>,
}

/// Reads locally cached route target statistics without request-path network I/O.
pub trait RouteTargetStatsReader: Send + Sync {
    /// Calculates statistics for `route_target_id` over the Router-selected `window`.
    ///
    /// Returns `None` when telemetry is unavailable or retained history cannot cover the window.
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

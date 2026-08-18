// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Bounded cumulative route-target snapshots and arbitrary-window statistics.

use std::collections::VecDeque;
use std::time::Duration;

use foretoken_model_protocol::{CumulativeHistogram, TelemetryResponse};
use foretoken_router::{RouteTargetLatencyStats, RouteTargetStats};

pub(crate) struct RouteTargetStatsHistory {
    retention: Duration,
    snapshots: VecDeque<TelemetryResponse>,
}

impl RouteTargetStatsHistory {
    pub(crate) fn new(retention: Duration) -> Self {
        Self {
            retention,
            snapshots: VecDeque::new(),
        }
    }

    pub(crate) fn push(&mut self, snapshot: TelemetryResponse) {
        if self.snapshots.back().is_some_and(|previous| {
            snapshot.collected_at_unix_ms <= previous.collected_at_unix_ms
                || counters_reset(previous, &snapshot)
        }) {
            self.snapshots.clear();
        }
        let newest = snapshot.collected_at_unix_ms;
        self.snapshots.push_back(snapshot);
        let retention_ms = u64::try_from(self.retention.as_millis()).unwrap_or(u64::MAX);
        while self
            .snapshots
            .front()
            .is_some_and(|oldest| newest.saturating_sub(oldest.collected_at_unix_ms) > retention_ms)
        {
            self.snapshots.pop_front();
        }
    }

    pub(crate) fn stats(&self, window: Duration) -> Option<RouteTargetStats> {
        let current = self.snapshots.back()?;
        let window_ms = u64::try_from(window.as_millis()).ok()?;
        let target = current.collected_at_unix_ms.checked_sub(window_ms)?;
        let baseline = self
            .snapshots
            .iter()
            .rev()
            .find(|snapshot| snapshot.collected_at_unix_ms <= target)?;
        let observed_ms = current
            .collected_at_unix_ms
            .checked_sub(baseline.collected_at_unix_ms)?;
        let observed_seconds = observed_ms as f64 / 1_000.0;
        if observed_seconds <= 0.0 {
            return None;
        }

        Some(RouteTargetStats {
            collected_at_unix_ms: current.collected_at_unix_ms,
            observed_window: Duration::from_millis(observed_ms),
            running_requests: current.running_requests,
            max_concurrent_requests: current.max_concurrent_requests,
            scheduler_running_requests: current.scheduler_running_requests,
            scheduler_waiting_requests: current.scheduler_waiting_requests,
            kv_cache_usage: current.kv_cache_usage,
            prompt_tokens_per_second: rate(
                baseline.prompt_tokens_total,
                current.prompt_tokens_total,
                observed_seconds,
            ),
            generation_tokens_per_second: rate(
                baseline.generation_tokens_total,
                current.generation_tokens_total,
                observed_seconds,
            ),
            ttft: latency(&baseline.ttft_seconds, &current.ttft_seconds),
            tpot: latency(&baseline.tpot_seconds, &current.tpot_seconds),
            e2e_latency: latency(&baseline.e2e_seconds, &current.e2e_seconds),
        })
    }
}

fn rate(previous: Option<u64>, current: Option<u64>, seconds: f64) -> Option<f64> {
    Some(current?.checked_sub(previous?)? as f64 / seconds)
}

fn latency(
    previous: &CumulativeHistogram,
    current: &CumulativeHistogram,
) -> Option<RouteTargetLatencyStats> {
    let samples = current.count.checked_sub(previous.count)?;
    if samples == 0 || current.buckets.len() != previous.buckets.len() {
        return None;
    }
    let sum_seconds = current.sum_seconds - previous.sum_seconds;
    if !sum_seconds.is_finite() || sum_seconds < 0.0 {
        return None;
    }
    let rank = samples.saturating_mul(95).div_ceil(100);
    let mut p95_ms = None;
    for (previous_bucket, current_bucket) in previous.buckets.iter().zip(&current.buckets) {
        if previous_bucket.le_seconds != current_bucket.le_seconds {
            return None;
        }
        if current_bucket.count.checked_sub(previous_bucket.count)? >= rank {
            p95_ms = Some(current_bucket.le_seconds * 1_000.0);
            break;
        }
    }
    Some(RouteTargetLatencyStats {
        samples,
        average_ms: sum_seconds * 1_000.0 / samples as f64,
        p95_ms,
    })
}

fn counters_reset(previous: &TelemetryResponse, current: &TelemetryResponse) -> bool {
    option_decreased(previous.prompt_tokens_total, current.prompt_tokens_total)
        || option_decreased(
            previous.generation_tokens_total,
            current.generation_tokens_total,
        )
        || histogram_reset(&previous.ttft_seconds, &current.ttft_seconds)
        || histogram_reset(&previous.tpot_seconds, &current.tpot_seconds)
        || histogram_reset(&previous.e2e_seconds, &current.e2e_seconds)
}

fn histogram_reset(previous: &CumulativeHistogram, current: &CumulativeHistogram) -> bool {
    current.count < previous.count
        || current.sum_seconds < previous.sum_seconds
        || current.buckets.len() != previous.buckets.len()
        || previous
            .buckets
            .iter()
            .zip(&current.buckets)
            .any(|(previous, current)| {
                current.le_seconds != previous.le_seconds || current.count < previous.count
            })
}

fn option_decreased(previous: Option<u64>, current: Option<u64>) -> bool {
    matches!((previous, current), (Some(previous), Some(current)) if current < previous)
}

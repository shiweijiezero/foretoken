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

#[cfg(test)]
mod tests {
    use foretoken_model_protocol::{CumulativeHistogramBucket, TelemetryResponse};

    use super::*;

    fn histogram(count: u64, sum_seconds: f64, first_bucket: u64) -> CumulativeHistogram {
        CumulativeHistogram {
            count,
            sum_seconds,
            buckets: vec![
                CumulativeHistogramBucket {
                    le_seconds: 0.1,
                    count: first_bucket,
                },
                CumulativeHistogramBucket {
                    le_seconds: 0.5,
                    count,
                },
            ],
        }
    }

    fn snapshot(at_ms: u64, tokens: u64, histogram: CumulativeHistogram) -> TelemetryResponse {
        TelemetryResponse {
            version: 2,
            collected_at_unix_ms: at_ms,
            accepting: true,
            running_requests: 3,
            max_concurrent_requests: 8,
            scheduler_running_requests: Some(2),
            scheduler_waiting_requests: Some(1),
            kv_cache_usage: Some(0.75),
            prompt_tokens_total: Some(tokens),
            generation_tokens_total: Some(tokens / 2),
            ttft_seconds: histogram.clone(),
            tpot_seconds: histogram.clone(),
            e2e_seconds: histogram,
        }
    }

    #[test]
    fn calculates_an_algorithm_selected_window_from_cumulative_snapshots() {
        let mut history = RouteTargetStatsHistory::new(Duration::from_secs(300));
        history.push(snapshot(1_000, 100, histogram(2, 0.2, 1)));
        history.push(snapshot(151_000, 400, histogram(4, 0.8, 2)));

        let stats = history.stats(Duration::from_secs(150)).unwrap();
        assert_eq!(stats.observed_window, Duration::from_secs(150));
        assert_eq!(stats.prompt_tokens_per_second, Some(2.0));
        assert_eq!(stats.generation_tokens_per_second, Some(1.0));
        assert_eq!(stats.ttft.unwrap().p95_ms, Some(500.0));
    }

    #[test]
    fn keeps_average_when_p95_exceeds_the_largest_bucket() {
        let mut history = RouteTargetStatsHistory::new(Duration::from_secs(300));
        history.push(snapshot(1_000, 100, histogram(1, 0.1, 1)));
        let mut overflow = histogram(2, 1.1, 1);
        overflow.buckets[1].count = 1;
        history.push(snapshot(151_000, 400, overflow));

        let latency = history
            .stats(Duration::from_secs(150))
            .unwrap()
            .ttft
            .unwrap();
        assert_eq!(latency.average_ms, 1_000.0);
        assert_eq!(latency.p95_ms, None);
    }

    #[test]
    fn clears_history_when_a_histogram_cumulative_value_resets() {
        let mut history = RouteTargetStatsHistory::new(Duration::from_secs(300));
        history.push(snapshot(1_000, 100, histogram(10, 5.0, 8)));
        history.push(snapshot(151_000, 400, histogram(10, 1.0, 2)));

        assert!(history.stats(Duration::from_secs(150)).is_none());
        assert!(histogram_reset(
            &histogram(10, 5.0, 8),
            &histogram(10, 5.0, 7),
        ));
    }

    #[test]
    fn returns_none_until_the_requested_window_is_available() {
        let mut history = RouteTargetStatsHistory::new(Duration::from_secs(300));
        history.push(snapshot(100_000, 100, histogram(1, 0.1, 1)));
        history.push(snapshot(200_000, 200, histogram(2, 0.2, 2)));

        assert!(history.stats(Duration::from_secs(150)).is_none());
    }
}

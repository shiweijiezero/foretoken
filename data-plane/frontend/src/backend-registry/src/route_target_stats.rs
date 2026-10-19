// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Bounded cumulative route-target snapshots and arbitrary-window statistics.

use std::collections::VecDeque;
use std::time::Duration;

use foretoken_model_protocol::{CumulativeHistogram, HistogramMoments, TelemetryResponse};
use foretoken_router::{RouteTargetLatencyStats, RouteTargetStats};

/// Registry-owned compact counters and their last calculated daily mean.
/// Updating on telemetry arrival keeps history scans off the request path.
#[derive(Default)]
struct RequestLengthHistory {
    samples: VecDeque<(u64, HistogramMoments)>,
    sum_delta: f64,
    count_delta: f64,
    mean: Option<f64>,
}

pub(crate) struct RouteTargetStatsHistory {
    retention: Duration,
    snapshots: VecDeque<TelemetryResponse>,
    prompt_lengths: RequestLengthHistory,
    generation_lengths: RequestLengthHistory,
}

impl RouteTargetStatsHistory {
    /// Creates short-window telemetry history and a separate 24-hour request-length history.
    ///
    /// `BackendRegistry` owns the history for one physical route target and appends successful probes.
    pub(crate) fn new(retention: Duration) -> Self {
        Self {
            retention,
            snapshots: VecDeque::new(),
            prompt_lengths: RequestLengthHistory::default(),
            generation_lengths: RequestLengthHistory::default(),
        }
    }

    /// Records one cumulative telemetry snapshot and evicts expired or reset history.
    ///
    /// Backend readiness refresh supplies the snapshot, which is stored in this bounded history.
    pub(crate) fn push(&mut self, snapshot: TelemetryResponse, request_lengths: bool) {
        // Keep only compact length counters for the daily window, not full telemetry snapshots.
        // Unlike short-window latency statistics, these histories account for counter resets.
        if self
            .snapshots
            .back()
            .is_some_and(|previous| snapshot.collected_at_unix_ms <= previous.collected_at_unix_ms)
        {
            self.clear_request_lengths();
        }
        if request_lengths {
            self.prompt_lengths.push(
                snapshot.collected_at_unix_ms,
                snapshot.request_cost.prompt_tokens,
            );
            self.generation_lengths.push(
                snapshot.collected_at_unix_ms,
                snapshot.request_cost.generation_tokens,
            );
        }
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

    /// Releases daily samples when the accepted routing configuration no longer consumes them.
    pub(crate) fn clear_request_lengths(&mut self) {
        self.prompt_lengths = RequestLengthHistory::default();
        self.generation_lengths = RequestLengthHistory::default();
    }

    /// Returns current gauges immediately and derives counter statistics when history covers the window.
    ///
    /// The registry exposes the returned snapshot to Router scorers; history ownership remains local.
    pub(crate) fn stats(&self, window: Duration) -> Option<RouteTargetStats> {
        let current = self.snapshots.back()?;
        let baseline = u64::try_from(window.as_millis())
            .ok()
            .and_then(|window_ms| current.collected_at_unix_ms.checked_sub(window_ms))
            .and_then(|target| {
                self.snapshots
                    .iter()
                    .rev()
                    .find(|snapshot| snapshot.collected_at_unix_ms <= target)
            });
        let observed_ms = baseline
            .and_then(|baseline| {
                current
                    .collected_at_unix_ms
                    .checked_sub(baseline.collected_at_unix_ms)
            })
            .filter(|milliseconds| *milliseconds > 0);
        let observed_seconds = observed_ms.map(|milliseconds| milliseconds as f64 / 1_000.0);

        Some(RouteTargetStats {
            data_parallel_ranks: current.data_parallel_ranks.clone(),
            prompt_tokens_per_request: self.prompt_lengths.mean,
            generation_tokens_per_request: self.generation_lengths.mean,
            collected_at_unix_ms: current.collected_at_unix_ms,
            observed_window: Duration::from_millis(observed_ms.unwrap_or(0)),
            running_requests: current.running_requests,
            max_concurrent_requests: current.max_concurrent_requests,
            scheduler_running_requests: self
                .snapshots
                .iter()
                .rev()
                .find_map(|snapshot| snapshot.scheduler_running_requests),
            scheduler_waiting_requests: self
                .snapshots
                .iter()
                .rev()
                .find_map(|snapshot| snapshot.scheduler_waiting_requests),
            kv_cache_usage: self
                .snapshots
                .iter()
                .rev()
                .find_map(|snapshot| snapshot.kv_cache_usage),
            prompt_tokens_per_second: baseline.zip(observed_seconds).and_then(
                |(baseline, seconds)| {
                    rate(
                        baseline.prompt_tokens_total,
                        current.prompt_tokens_total,
                        seconds,
                    )
                },
            ),
            generation_tokens_per_second: baseline.zip(observed_seconds).and_then(
                |(baseline, seconds)| {
                    rate(
                        baseline.generation_tokens_total,
                        current.generation_tokens_total,
                        seconds,
                    )
                },
            ),
            ttft: baseline
                .and_then(|baseline| latency(&baseline.ttft_seconds, &current.ttft_seconds)),
            tpot: baseline
                .and_then(|baseline| latency(&baseline.tpot_seconds, &current.tpot_seconds)),
            e2e_latency: baseline
                .and_then(|baseline| latency(&baseline.e2e_seconds, &current.e2e_seconds)),
        })
    }
}

impl RequestLengthHistory {
    /// Retains the open-left 24-hour range and derives increase(sum) / increase(count).
    fn push(&mut self, now: u64, value: Option<HistogramMoments>) {
        const WINDOW_MS: u64 = 24 * 60 * 60 * 1_000;
        if let Some(value) = value {
            if let Some((_, previous)) = self.samples.back() {
                self.sum_delta += counter_step(previous.sum, value.sum);
                self.count_delta += counter_step(previous.count as f64, value.count as f64);
            }
            self.samples.push_back((now, value));
        }
        // Each corrected counter edge enters and leaves the window once.
        while self
            .samples
            .front()
            .is_some_and(|(at, _)| now - at >= WINDOW_MS)
        {
            let (_, removed) = self.samples.pop_front().expect("window has a first sample");
            if let Some((_, next)) = self.samples.front() {
                self.sum_delta -= counter_step(removed.sum, next.sum);
                self.count_delta -= counter_step(removed.count as f64, next.count as f64);
            }
        }
        self.mean = if self.samples.len() < 2 {
            self.sum_delta = 0.0;
            self.count_delta = 0.0;
            None
        } else if self.count_delta == 0.0 {
            None
        } else {
            Some(
                self.increase(now, WINDOW_MS, self.sum_delta, |value| value.sum)
                    / self.increase(now, WINDOW_MS, self.count_delta, |value| value.count as f64),
            )
        };
    }

    /// Applies PromQL float-counter reset correction and boundary extrapolation to local samples.
    /// Telemetry has no counter start timestamps; only observed decreases identify resets.
    fn increase(
        &self,
        now: u64,
        window_ms: u64,
        delta: f64,
        counter: impl Fn(&HistogramMoments) -> f64,
    ) -> f64 {
        let (first_at, first) = self.samples.front().expect("at least two counter samples");
        let (last_at, _) = self.samples.back().expect("at least two counter samples");
        let first_value = counter(first);
        let interval = (last_at - first_at) as f64 / 1_000.0;
        let spacing = interval / (self.samples.len() - 1) as f64;
        let mut to_start = (window_ms - (now - first_at)) as f64 / 1_000.0;
        let mut to_end = (now - last_at) as f64 / 1_000.0;
        if to_start >= spacing * 1.1 {
            to_start = spacing / 2.0;
        }
        if delta > 0.0 && first_value >= 0.0 {
            to_start = to_start.min(interval * (first_value / delta));
        }
        if to_end >= spacing * 1.1 {
            to_end = spacing / 2.0;
        }
        delta * ((interval + to_start + to_end) / interval)
    }
}

// A counter reset contributes only the observations collected since its restart.
fn counter_step(previous: f64, current: f64) -> f64 {
    if current < previous {
        current
    } else {
        current - previous
    }
}

fn rate(previous: Option<u64>, current: Option<u64>, seconds: f64) -> Option<f64> {
    Some(current?.checked_sub(previous?)? as f64 / seconds)
}

// Derives one window-local mean and bucket p95 from cumulative telemetry, rejecting resets or
// incompatible bucket layouts rather than combining observations from different histogram epochs.
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

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Typed reads of vLLM metrics and cumulative model-server latency observations.

use foretoken_model_protocol::{
    CumulativeHistogram, CumulativeHistogramBucket, DataParallelTelemetry,
};
use vllm_metrics::{EngineLabels, METRICS};

// The selected vLLM crate does not expose its request histogram boundaries. Keep the compatible
// resolution here until that upstream API exists; telemetry carries the actual boundaries.
const TTFT_BUCKETS_SECONDS: &[f64] = &[
    0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0,
    20.0, 40.0, 80.0, 160.0, 640.0, 2560.0,
];
const TPOT_BUCKETS_SECONDS: &[f64] = &[
    0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.01, 0.015, 0.02, 0.025, 0.03,
    0.04, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0, 20.0, 40.0,
    80.0,
];
const E2E_BUCKETS_SECONDS: &[f64] = &[
    0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 5.0, 10.0, 15.0, 20.0, 30.0, 40.0, 50.0, 60.0, 120.0, 240.0,
    480.0, 960.0, 1920.0, 7680.0,
];

struct BoundaryHistogram {
    boundaries: &'static [f64],
    cumulative_counts: Vec<u64>,
    count: u64,
    sum_seconds: f64,
}

impl BoundaryHistogram {
    fn new(boundaries: &'static [f64]) -> Self {
        Self {
            boundaries,
            cumulative_counts: vec![0; boundaries.len()],
            count: 0,
            sum_seconds: 0.0,
        }
    }

    fn observe(&mut self, seconds: f64) {
        self.count += 1;
        self.sum_seconds += seconds;
        for (boundary, count) in self.boundaries.iter().zip(&mut self.cumulative_counts) {
            if seconds <= *boundary {
                *count += 1;
            }
        }
    }

    fn snapshot(&self) -> CumulativeHistogram {
        CumulativeHistogram {
            count: self.count,
            sum_seconds: self.sum_seconds,
            buckets: self
                .boundaries
                .iter()
                .zip(&self.cumulative_counts)
                .map(|(le_seconds, count)| CumulativeHistogramBucket {
                    le_seconds: *le_seconds,
                    count: *count,
                })
                .collect(),
        }
    }
}

pub(crate) struct BoundaryLatencyMetrics {
    ttft: BoundaryHistogram,
    tpot: BoundaryHistogram,
    e2e: BoundaryHistogram,
}

impl BoundaryLatencyMetrics {
    /// Creates empty latency accumulators owned by the vLLM backend adapter.
    pub(crate) fn new() -> Self {
        Self {
            ttft: BoundaryHistogram::new(TTFT_BUCKETS_SECONDS),
            tpot: BoundaryHistogram::new(TPOT_BUCKETS_SECONDS),
            e2e: BoundaryHistogram::new(E2E_BUCKETS_SECONDS),
        }
    }

    /// Records one engine-boundary TTFT sample for the stream adapter's telemetry snapshot.
    pub(crate) fn observe_ttft(&mut self, seconds: f64) {
        self.ttft.observe(seconds);
    }

    /// Records one engine-boundary TPOT sample for the stream adapter's telemetry snapshot.
    pub(crate) fn observe_tpot(&mut self, seconds: f64) {
        self.tpot.observe(seconds);
    }

    /// Records one engine-boundary end-to-end sample for the stream adapter's telemetry snapshot.
    pub(crate) fn observe_e2e(&mut self, seconds: f64) {
        self.e2e.observe(seconds);
    }

    /// Returns owned cumulative histograms for the backend telemetry publisher without resetting them.
    pub(crate) fn snapshot(
        &self,
    ) -> (
        CumulativeHistogram,
        CumulativeHistogram,
        CumulativeHistogram,
    ) {
        (
            self.ttft.snapshot(),
            self.tpot.snapshot(),
            self.e2e.snapshot(),
        )
    }
}

pub(crate) struct VllmMetricsSnapshot {
    pub(crate) data_parallel_ranks: Vec<DataParallelTelemetry>,
    pub(crate) scheduler_running_requests: Option<u64>,
    pub(crate) scheduler_waiting_requests: Option<u64>,
    pub(crate) kv_cache_usage: Option<f64>,
    pub(crate) prompt_tokens_total: Option<u64>,
    pub(crate) generation_tokens_total: Option<u64>,
}

/// Reads the selected EngineCore metrics for `VllmBackend::telemetry` without retaining labels.
///
/// Returns rank-local scheduler gauges and their group aggregate in one owned snapshot.
pub(crate) fn read_vllm_metrics(engine_labels: &[EngineLabels]) -> VllmMetricsSnapshot {
    let data_parallel_ranks = engine_labels
        .iter()
        .map(|labels| DataParallelTelemetry {
            data_parallel_rank: labels.engine,
            scheduler_running_requests: METRICS
                .scheduler
                .scheduler_running
                .get(labels)
                .map(|metric| metric.get()),
            scheduler_waiting_requests: METRICS
                .scheduler
                .scheduler_waiting
                .get(labels)
                .map(|metric| metric.get()),
            kv_cache_usage: METRICS
                .scheduler
                .kv_cache_usage
                .get(labels)
                .map(|metric| metric.get()),
        })
        .collect::<Vec<_>>();
    // Aggregate exactly the same rank observations used by the router; missing ranks stay unknown.
    let scheduler_running_requests =
        sum_metric(&data_parallel_ranks, |rank| rank.scheduler_running_requests);
    let scheduler_waiting_requests =
        sum_metric(&data_parallel_ranks, |rank| rank.scheduler_waiting_requests);
    let kv_cache_usage = if data_parallel_ranks.is_empty() {
        None
    } else {
        data_parallel_ranks
            .iter()
            .try_fold(0.0, |total, rank| Some(total + rank.kv_cache_usage?))
            .map(|total| total / data_parallel_ranks.len() as f64)
    };
    VllmMetricsSnapshot {
        data_parallel_ranks,
        scheduler_running_requests,
        scheduler_waiting_requests,
        kv_cache_usage,
        prompt_tokens_total: sum_metric(engine_labels, |labels| {
            METRICS
                .request
                .prompt_tokens
                .get(labels)
                .map(|metric| metric.get())
        }),
        generation_tokens_total: sum_metric(engine_labels, |labels| {
            METRICS
                .request
                .generation_tokens
                .get(labels)
                .map(|metric| metric.get())
        }),
    }
}

fn sum_metric<T>(observations: &[T], read: impl Fn(&T) -> Option<u64>) -> Option<u64> {
    if observations.is_empty() {
        return None;
    }
    observations
        .iter()
        .map(read)
        .try_fold(0_u64, |total, value| total.checked_add(value?))
}

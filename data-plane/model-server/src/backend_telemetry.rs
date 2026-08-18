// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Typed reads of vLLM metrics and cumulative model-server latency observations.

use foretoken_model_protocol::{CumulativeHistogram, CumulativeHistogramBucket};
use vllm_metrics::{EngineLabels, METRICS};

// These boundaries match the selected local vLLM build source's request metrics. They are observed
// locally because prometheus-client exposes no production histogram snapshot API.
const TTFT_BUCKETS: [f64; 22] = [
    0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0,
    20.0, 40.0, 80.0, 160.0, 640.0, 2560.0,
];
const TPOT_BUCKETS: [f64; 19] = [
    0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0, 20.0,
    40.0, 80.0,
];
const E2E_BUCKETS: [f64; 21] = [
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
    pub(crate) fn new() -> Self {
        Self {
            ttft: BoundaryHistogram::new(&TTFT_BUCKETS),
            tpot: BoundaryHistogram::new(&TPOT_BUCKETS),
            e2e: BoundaryHistogram::new(&E2E_BUCKETS),
        }
    }

    pub(crate) fn observe_ttft(&mut self, seconds: f64) {
        self.ttft.observe(seconds);
    }

    pub(crate) fn observe_tpot(&mut self, seconds: f64) {
        self.tpot.observe(seconds);
    }

    pub(crate) fn observe_e2e(&mut self, seconds: f64) {
        self.e2e.observe(seconds);
    }

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
    pub(crate) scheduler_running_requests: Option<u64>,
    pub(crate) scheduler_waiting_requests: Option<u64>,
    pub(crate) kv_cache_usage: Option<f64>,
    pub(crate) prompt_tokens_total: Option<u64>,
    pub(crate) generation_tokens_total: Option<u64>,
}

pub(crate) fn read_vllm_metrics(engine_labels: &[EngineLabels]) -> VllmMetricsSnapshot {
    VllmMetricsSnapshot {
        scheduler_running_requests: sum_engine_metric(engine_labels, |labels| {
            METRICS
                .scheduler
                .scheduler_running
                .get(labels)
                .map(|metric| metric.get())
        }),
        scheduler_waiting_requests: sum_engine_metric(engine_labels, |labels| {
            METRICS
                .scheduler
                .scheduler_waiting
                .get(labels)
                .map(|metric| metric.get())
        }),
        kv_cache_usage: average_engine_metric(engine_labels, |labels| {
            METRICS
                .scheduler
                .kv_cache_usage
                .get(labels)
                .map(|metric| metric.get())
        }),
        prompt_tokens_total: sum_engine_metric(engine_labels, |labels| {
            METRICS
                .request
                .prompt_tokens
                .get(labels)
                .map(|metric| metric.get())
        }),
        generation_tokens_total: sum_engine_metric(engine_labels, |labels| {
            METRICS
                .request
                .generation_tokens
                .get(labels)
                .map(|metric| metric.get())
        }),
    }
}

fn sum_engine_metric(
    engine_labels: &[EngineLabels],
    read: impl Fn(&EngineLabels) -> Option<u64>,
) -> Option<u64> {
    if engine_labels.is_empty() {
        return None;
    }
    engine_labels
        .iter()
        .map(read)
        .try_fold(0_u64, |total, value| total.checked_add(value?))
}

fn average_engine_metric(
    engine_labels: &[EngineLabels],
    read: impl Fn(&EngineLabels) -> Option<f64>,
) -> Option<f64> {
    if engine_labels.is_empty() {
        return None;
    }
    let total = engine_labels
        .iter()
        .map(read)
        .try_fold(0.0, |total, value| Some(total + value?))?;
    Some(total / engine_labels.len() as f64)
}

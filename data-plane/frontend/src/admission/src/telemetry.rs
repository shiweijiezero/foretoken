// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Admission-call results and queue timing, separate from reservation ownership.

use std::collections::HashMap;
use std::fmt;
use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

use prometheus_client::encoding::EncodeLabelSet;
use prometheus_client::encoding::text::encode;
use prometheus_client::metrics::counter::Counter;
use prometheus_client::metrics::family::Family;
use prometheus_client::metrics::gauge::Gauge;
use prometheus_client::metrics::histogram::{Histogram, exponential_buckets};
use prometheus_client::registry::Registry;

use super::AdmissionError;

#[derive(Clone)]
struct HttpObservation {
    deadline_elapsed: Arc<AtomicBool>,
}

tokio::task_local! {
    static HTTP_OBSERVATION: HttpObservation;
}

/// Runs HTTP admission and its handlers with a shared observation-only deadline cause.
pub async fn observe_http<T>(work: impl Future<Output = T>) -> T {
    HTTP_OBSERVATION
        .scope(
            HttpObservation {
                deadline_elapsed: Arc::new(AtomicBool::new(false)),
            },
            work,
        )
        .await
}

/// Records the framework's deadline before it cancels nested admission futures.
pub fn mark_request_deadline() {
    let _ = HTTP_OBSERVATION.try_with(|observation| {
        observation.deadline_elapsed.store(true, Ordering::Relaxed);
    });
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct CallLabels {
    stage: &'static str,
    origin: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ResultLabels {
    stage: &'static str,
    origin: &'static str,
    result: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct WaitLabels {
    origin: &'static str,
    result: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct AlgorithmLabels {
    algorithm: &'static str,
}

#[derive(Clone, Debug, Default, Hash, PartialEq, Eq, EncodeLabelSet)]
struct NoLabels {}

#[derive(Default)]
struct MetricOwners {
    algorithms: HashMap<&'static str, usize>,
    bounded: usize,
}

pub(crate) struct AdmissionMetrics {
    registry: Registry,
    owners: Mutex<MetricOwners>,
    info: Family<AlgorithmLabels, Gauge>,
    concurrency_limit: Family<NoLabels, Gauge>,
    queue_limit: Family<NoLabels, Gauge>,
    resident_limit: Family<NoLabels, Gauge>,
    pub(crate) active: Gauge,
    pub(crate) queued: Gauge,
    pub(crate) resident: Gauge,
    attempts: Family<CallLabels, Counter>,
    results: Family<ResultLabels, Counter>,
    wait: Family<WaitLabels, Histogram, fn() -> Histogram>,
}

fn wait_histogram() -> Histogram {
    Histogram::new(exponential_buckets(0.001, 2.0, 20))
}

impl AdmissionMetrics {
    fn new() -> Self {
        let mut metrics = Self {
            registry: Registry::default(),
            owners: Mutex::new(MetricOwners::default()),
            info: Family::default(),
            concurrency_limit: Family::default(),
            queue_limit: Family::default(),
            resident_limit: Family::default(),
            active: Gauge::default(),
            queued: Gauge::default(),
            resident: Gauge::default(),
            attempts: Family::default(),
            results: Family::default(),
            wait: Family::new_with_constructor(wait_histogram as fn() -> Histogram),
        };
        metrics.registry.register(
            "foretoken_admission_info",
            "Configured admission algorithm",
            metrics.info.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_concurrency_limit_work_units",
            "Running admission work-unit limit",
            metrics.concurrency_limit.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_queue_limit_work_units",
            "Waiting admission work-unit limit",
            metrics.queue_limit.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_resident_limit_requests",
            "Resident protected HTTP request limit",
            metrics.resident_limit.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_active_work_units",
            "Work units retaining admission reservations",
            metrics.active.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_queued_work_units",
            "Work units waiting for admission",
            metrics.queued.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_resident_requests",
            "Resident protected HTTP requests",
            metrics.resident.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_attempts",
            "Admission calls started, not candidate units",
            metrics.attempts.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_results",
            "Completed admission calls by result",
            metrics.results.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_queue_wait_seconds",
            "Completed queue waits by admission result",
            metrics.wait.clone(),
        );
        metrics
    }

    fn initialize_calls(&self) {
        for (stage, origin) in [("intake", "http"), ("work", "http"), ("work", "internal")] {
            self.attempts
                .get_or_create(&CallLabels { stage, origin })
                .inc_by(0);
            self.results
                .get_or_create(&ResultLabels {
                    stage,
                    origin,
                    result: "admitted",
                })
                .inc_by(0);
        }
    }
}

/// Finite resources advertised by the running admission rule.
#[derive(Clone, Copy, Debug)]
pub struct AdmissionCapacity {
    pub concurrent_work_units: u32,
    pub queued_work_units: u32,
    pub resident_requests: u64,
}

/// Keeps configured-rule series present for the owning admission instance's lifetime.
pub(crate) struct AdmissionMetricsScope {
    algorithm: &'static str,
    capacity: Option<AdmissionCapacity>,
}

impl AdmissionMetricsScope {
    pub(crate) fn new(algorithm: &'static str, capacity: Option<AdmissionCapacity>) -> Self {
        let metrics = &METRICS;
        let mut owners = metrics
            .owners
            .lock()
            .expect("admission metric owners lock poisoned");
        metrics.initialize_calls();
        *owners.algorithms.entry(algorithm).or_default() += 1;
        metrics
            .info
            .get_or_create(&AlgorithmLabels { algorithm })
            .set(1);
        if let Some(capacity) = capacity {
            owners.bounded += 1;
            metrics
                .concurrency_limit
                .get_or_create(&NoLabels {})
                .inc_by(i64::from(capacity.concurrent_work_units));
            metrics
                .queue_limit
                .get_or_create(&NoLabels {})
                .inc_by(i64::from(capacity.queued_work_units));
            metrics
                .resident_limit
                .get_or_create(&NoLabels {})
                .inc_by(capacity.resident_requests as i64);
        }
        Self {
            algorithm,
            capacity,
        }
    }
}

impl Drop for AdmissionMetricsScope {
    fn drop(&mut self) {
        let metrics = &METRICS;
        let mut owners = metrics
            .owners
            .lock()
            .expect("admission metric owners lock poisoned");
        let count = owners
            .algorithms
            .get_mut(self.algorithm)
            .expect("registered admission metric owner");
        *count -= 1;
        if *count == 0 {
            owners.algorithms.remove(self.algorithm);
            metrics.info.remove(&AlgorithmLabels {
                algorithm: self.algorithm,
            });
        }
        if let Some(capacity) = self.capacity {
            metrics
                .concurrency_limit
                .get_or_create(&NoLabels {})
                .dec_by(i64::from(capacity.concurrent_work_units));
            metrics
                .queue_limit
                .get_or_create(&NoLabels {})
                .dec_by(i64::from(capacity.queued_work_units));
            metrics
                .resident_limit
                .get_or_create(&NoLabels {})
                .dec_by(capacity.resident_requests as i64);
            owners.bounded -= 1;
            if owners.bounded == 0 {
                metrics.concurrency_limit.remove(&NoLabels {});
                metrics.queue_limit.remove(&NoLabels {});
                metrics.resident_limit.remove(&NoLabels {});
            }
        }
    }
}

#[derive(Default)]
struct QueueTiming {
    started: Option<Instant>,
    elapsed: Option<Duration>,
}

/// Queue timing handle supplied by the framework; the rule continues to own queue resources.
#[derive(Clone, Default)]
pub struct AdmissionQueueObservation(Arc<Mutex<QueueTiming>>);

impl AdmissionQueueObservation {
    /// Starts this call's queue observation, ending when the returned guard is dropped.
    pub fn begin_wait(&self) -> AdmissionQueueWait {
        self.0
            .lock()
            .expect("admission queue observation lock poisoned")
            .started = Some(Instant::now());
        AdmissionQueueWait(self.clone())
    }
}

/// Captures elapsed queue time without deciding the admission result.
pub struct AdmissionQueueWait(AdmissionQueueObservation);

impl Drop for AdmissionQueueWait {
    fn drop(&mut self) {
        let mut timing = self
            .0
            .0
            .lock()
            .expect("admission queue observation lock poisoned");
        timing.elapsed = timing.started.map(|started| started.elapsed());
    }
}

/// Observes one intake or work admission call and emits exactly one final result.
/// Resource permits have their own lifetime and never update this result.
pub struct AdmissionAttempt {
    labels: CallLabels,
    deadline: Option<tokio::time::Instant>,
    deadline_elapsed: Option<Arc<AtomicBool>>,
    queue: AdmissionQueueObservation,
    finished: bool,
}

impl AdmissionAttempt {
    /// Starts the immediate HTTP intake decision before body extraction.
    pub fn intake() -> Self {
        Self::start("intake", "http", None)
    }

    /// Starts a complete work admission, deriving its origin from the framework HTTP scope.
    pub fn work(deadline: tokio::time::Instant) -> Self {
        let origin = if HTTP_OBSERVATION.try_with(|_| ()).is_ok() {
            "http"
        } else {
            "internal"
        };
        Self::start("work", origin, Some(deadline))
    }

    fn start(
        stage: &'static str,
        origin: &'static str,
        deadline: Option<tokio::time::Instant>,
    ) -> Self {
        let labels = CallLabels { stage, origin };
        METRICS.attempts.get_or_create(&labels).inc();
        Self {
            labels,
            deadline,
            deadline_elapsed: HTTP_OBSERVATION
                .try_with(|scope| scope.deadline_elapsed.clone())
                .ok(),
            queue: AdmissionQueueObservation::default(),
            finished: false,
        }
    }

    /// Supplies the rule with this attempt's waiting observation.
    pub fn queue(&self) -> AdmissionQueueObservation {
        self.queue.clone()
    }

    /// Runs work admission under the original budget and records its final decision.
    pub async fn run<T>(
        self,
        work: impl Future<Output = Result<T, AdmissionError>>,
    ) -> Result<T, AdmissionError> {
        let deadline = self
            .deadline
            .expect("work admission has a request deadline");
        let result = tokio::select! {
            biased;
            _ = tokio::time::sleep_until(deadline) => Err(AdmissionError::DeadlineExceeded),
            result = work => {
                if tokio::time::Instant::now() >= deadline {
                    Err(AdmissionError::DeadlineExceeded)
                } else {
                    result
                }
            }
        };
        self.complete(&result);
        result
    }

    /// Records the framework's final result after its deadline check.
    pub fn complete<T>(mut self, result: &Result<T, AdmissionError>) {
        let result = match result {
            Ok(_) => "admitted",
            Err(AdmissionError::Overloaded) => "capacity_rejected",
            Err(AdmissionError::QueueTimeout) => "queue_timeout",
            Err(AdmissionError::DeadlineExceeded) => "deadline_exceeded",
            Err(AdmissionError::BatchTooLarge) => "invalid_request",
            Err(AdmissionError::Closed) => "closed",
        };
        self.record(result);
    }

    fn record(&mut self, result: &'static str) {
        let metrics = &METRICS;
        metrics
            .results
            .get_or_create(&ResultLabels {
                stage: self.labels.stage,
                origin: self.labels.origin,
                result,
            })
            .inc();
        let timing = self
            .queue
            .0
            .lock()
            .expect("admission queue observation lock poisoned");
        if let Some(elapsed) = timing
            .elapsed
            .or_else(|| timing.started.map(|started| started.elapsed()))
        {
            metrics
                .wait
                .get_or_create(&WaitLabels {
                    origin: self.labels.origin,
                    result,
                })
                .observe(elapsed.as_secs_f64());
        }
        self.finished = true;
    }
}

impl Drop for AdmissionAttempt {
    fn drop(&mut self) {
        if !self.finished {
            // An outer request timer can drop this future before the inner timer is polled.
            let expired = self
                .deadline_elapsed
                .as_ref()
                .is_some_and(|flag| flag.load(Ordering::Relaxed))
                || self
                    .deadline
                    .is_some_and(|deadline| tokio::time::Instant::now() >= deadline);
            self.record(if expired {
                "deadline_exceeded"
            } else {
                "cancelled"
            });
        }
    }
}

pub(crate) static METRICS: LazyLock<AdmissionMetrics> = LazyLock::new(AdmissionMetrics::new);

/// Encodes admission metrics for the frontend's combined OpenMetrics response.
pub fn render_metrics() -> Result<String, fmt::Error> {
    let mut output = String::new();
    encode(&mut output, &METRICS.registry)?;
    Ok(output)
}

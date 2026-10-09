// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Admission-call results and queue timing, separate from reservation ownership.

use std::collections::BTreeSet;
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
    model_name: String,
    origin: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ResultLabels {
    model_name: String,
    origin: &'static str,
    result: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct AlgorithmLabels {
    model_name: String,
    algorithm: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ModelLabels {
    model_name: String,
}

struct AdmissionMetrics {
    registry: Registry,
    catalog: Mutex<BTreeSet<String>>,
    model_info: Family<ModelLabels, Gauge>,
    draining: Family<ModelLabels, Gauge>,
    configured_models: Gauge,
    info: Family<AlgorithmLabels, Gauge>,
    concurrency_limit: Family<ModelLabels, Gauge>,
    queue_limit: Family<ModelLabels, Gauge>,
    active: Family<ModelLabels, Gauge>,
    queued: Family<ModelLabels, Gauge>,
    attempts: Family<CallLabels, Counter>,
    results: Family<ResultLabels, Counter>,
    wait: Family<ResultLabels, Histogram, fn() -> Histogram>,
}

/// Model-local resource counters retained by algorithm-owned reservations.
#[derive(Clone)]
pub struct AdmissionMetricsHandle {
    pub active: Gauge,
    pub queued: Gauge,
}

fn wait_histogram() -> Histogram {
    Histogram::new(exponential_buckets(0.001, 2.0, 20))
}

impl AdmissionMetrics {
    fn new() -> Self {
        let mut metrics = Self {
            registry: Registry::default(),
            catalog: Mutex::new(BTreeSet::new()),
            model_info: Family::default(),
            draining: Family::default(),
            configured_models: Gauge::default(),
            info: Family::default(),
            concurrency_limit: Family::default(),
            queue_limit: Family::default(),
            active: Family::default(),
            queued: Family::default(),
            attempts: Family::default(),
            results: Family::default(),
            wait: Family::new_with_constructor(wait_histogram as fn() -> Histogram),
        };
        metrics.registry.register(
            "foretoken_admission_model_info",
            "Models in the currently published serving catalog",
            metrics.model_info.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_draining",
            "Whether the effective model rule is draining outstanding work",
            metrics.draining.clone(),
        );
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
            "foretoken_admission_configured_models",
            "Models in the currently published serving catalog",
            metrics.configured_models.clone(),
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

    /// Publishes zero-event baselines for both framework call origins before traffic arrives.
    fn initialize_calls(&self, model: &str) {
        for origin in ["http", "internal"] {
            self.attempts
                .get_or_create(&CallLabels {
                    model_name: model.into(),
                    origin,
                })
                .inc_by(0);
            self.results
                .get_or_create(&ResultLabels {
                    model_name: model.into(),
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
}

/// Keeps model-local rule series present for the owning admission instance's lifetime.
pub(crate) struct AdmissionMetricsScope {
    model: ModelLabels,
    algorithm: &'static str,
    bounded: bool,
    handle: AdmissionMetricsHandle,
}

impl AdmissionMetricsScope {
    /// Publishes an effective rule after the registry has committed its activation.
    pub(crate) fn new(
        model: &str,
        algorithm: &'static str,
        capacity: Option<AdmissionCapacity>,
    ) -> Self {
        let metrics = &METRICS;
        let model = ModelLabels {
            model_name: model.into(),
        };
        metrics
            .info
            .get_or_create(&AlgorithmLabels {
                model_name: model.model_name.clone(),
                algorithm,
            })
            .set(1);
        metrics.draining.get_or_create(&model).set(0);
        // The registry owns exactly one effective rule per model, including during a drain.
        let handle = if let Some(capacity) = capacity {
            metrics
                .concurrency_limit
                .get_or_create(&model)
                .set(i64::from(capacity.concurrent_work_units));
            metrics
                .queue_limit
                .get_or_create(&model)
                .set(i64::from(capacity.queued_work_units));
            AdmissionMetricsHandle {
                active: metrics.active.get_or_create(&model).clone(),
                queued: metrics.queued.get_or_create(&model).clone(),
            }
        } else {
            AdmissionMetricsHandle {
                active: Gauge::default(),
                queued: Gauge::default(),
            }
        };
        Self {
            model,
            algorithm,
            bounded: capacity.is_some(),
            handle,
        }
    }

    /// Supplies resource gauges that reservations retain until their work ends.
    pub(crate) fn metrics(&self) -> AdmissionMetricsHandle {
        self.handle.clone()
    }

    /// Publishes the registry's handover state without exposing pending rule limits.
    pub(crate) fn set_draining(&self, draining: bool) {
        METRICS
            .draining
            .get_or_create(&self.model)
            .set(i64::from(draining));
    }
}

impl Drop for AdmissionMetricsScope {
    fn drop(&mut self) {
        // The registry retires this scope only after its attempts and reservations finish.
        let metrics = &METRICS;
        metrics.info.remove(&AlgorithmLabels {
            model_name: self.model.model_name.clone(),
            algorithm: self.algorithm,
        });
        metrics.draining.remove(&self.model);
        if self.bounded {
            metrics.concurrency_limit.remove(&self.model);
            metrics.queue_limit.remove(&self.model);
            metrics.active.remove(&self.model);
            metrics.queued.remove(&self.model);
        }
    }
}

/// Cumulative events belong to the model, so rule replacement preserves unsampled results.
pub(crate) struct AdmissionModelMetrics {
    model: ModelLabels,
}

impl AdmissionModelMetrics {
    pub(crate) fn new(model: &str) -> Self {
        METRICS.initialize_calls(model);
        Self {
            model: ModelLabels {
                model_name: model.into(),
            },
        }
    }
}

impl Drop for AdmissionModelMetrics {
    fn drop(&mut self) {
        let metrics = &METRICS;
        for origin in ["http", "internal"] {
            metrics.attempts.remove(&CallLabels {
                model_name: self.model.model_name.clone(),
                origin,
            });
            for result in [
                "admitted",
                "capacity_rejected",
                "queue_timeout",
                "deadline_exceeded",
                "invalid_request",
                "closed",
                "cancelled",
            ] {
                let labels = ResultLabels {
                    model_name: self.model.model_name.clone(),
                    origin,
                    result,
                };
                metrics.results.remove(&labels);
                metrics.wait.remove(&labels);
            }
        }
    }
}

/// Publishes model inventory from the committed catalog, independently of effective rule telemetry.
pub(crate) fn set_configured_models(models: &[String]) {
    let metrics = &METRICS;
    let mut catalog = metrics
        .catalog
        .lock()
        .expect("admission model catalog lock poisoned");
    let next: BTreeSet<String> = models.iter().cloned().collect();
    for model in catalog.difference(&next) {
        metrics.model_info.remove(&ModelLabels {
            model_name: model.clone(),
        });
    }
    for model in &next {
        metrics
            .model_info
            .get_or_create(&ModelLabels {
                model_name: model.clone(),
            })
            .set(1);
    }
    metrics.configured_models.set(next.len() as i64);
    *catalog = next;
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

/// Observes one model's work admission call and emits exactly one final result.
/// Resource permits have their own lifetime and never update this result.
pub struct AdmissionAttempt {
    labels: CallLabels,
    deadline: tokio::time::Instant,
    deadline_elapsed: Option<Arc<AtomicBool>>,
    queue: AdmissionQueueObservation,
    finished: bool,
}

impl AdmissionAttempt {
    /// Starts a configured model's work admission, deriving origin from the framework HTTP scope.
    pub fn work(model: &str, deadline: tokio::time::Instant) -> Self {
        let origin = if HTTP_OBSERVATION.try_with(|_| ()).is_ok() {
            "http"
        } else {
            "internal"
        };
        let labels = CallLabels {
            model_name: model.into(),
            origin,
        };
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

    /// Runs the registry's admission decision under the original budget and records its result.
    pub async fn run<T>(
        self,
        work: impl Future<Output = Result<T, AdmissionError>>,
    ) -> Result<T, AdmissionError> {
        let deadline = self.deadline;
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
                model_name: self.labels.model_name.clone(),
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
                .get_or_create(&ResultLabels {
                    model_name: self.labels.model_name.clone(),
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
                || tokio::time::Instant::now() >= self.deadline;
            self.record(if expired {
                "deadline_exceeded"
            } else {
                "cancelled"
            });
        }
    }
}

static METRICS: LazyLock<AdmissionMetrics> = LazyLock::new(AdmissionMetrics::new);

/// Encodes admission metrics for the frontend's combined OpenMetrics response.
pub fn render_metrics() -> Result<String, fmt::Error> {
    let mut output = String::new();
    encode(&mut output, &METRICS.registry)?;
    Ok(output)
}

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared ledger snapshots and request outcomes through backend acceptance or CPU preparation.

use std::collections::BTreeMap;
use std::fmt;
use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::Instant;

use prometheus_client::encoding::EncodeLabelSet;
use prometheus_client::encoding::text::encode;
use prometheus_client::metrics::counter::Counter;
use prometheus_client::metrics::family::Family;
use prometheus_client::metrics::gauge::Gauge;
use prometheus_client::metrics::histogram::{Histogram, exponential_buckets};
use prometheus_client::registry::Registry;

use super::{AdmissionConfig, AdmissionError};

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
struct ModelLabels {
    model_name: String,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct SharedLabels {
    model_name: String,
    frontend_service_uid: String,
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

const RESULTS: [&str; 10] = [
    "dispatched",
    "prepared",
    "capacity_rejected",
    "queue_timeout",
    "deadline_exceeded",
    "invalid_request",
    "closed",
    "store_unavailable",
    "dispatch_failed",
    "cancelled",
];

struct AdmissionMetrics {
    registry: Registry,
    // Publication owns series lifetime. The lock also excludes late events during retirement.
    catalog: Mutex<BTreeMap<String, Option<SharedLabels>>>,
    model_info: Family<ModelLabels, Gauge>,
    configured_models: Gauge,
    enabled: Family<ModelLabels, Gauge>,
    waiting_limit: Family<SharedLabels, Gauge>,
    waiting: Family<SharedLabels, Gauge>,
    active: Family<SharedLabels, Gauge>,
    store_available: Family<SharedLabels, Gauge>,
    attempts: Family<CallLabels, Counter>,
    enqueued: Family<CallLabels, Counter>,
    results: Family<ResultLabels, Counter>,
    wait: Family<ResultLabels, Histogram, fn() -> Histogram>,
}

fn wait_histogram() -> Histogram {
    Histogram::new(exponential_buckets(0.001, 2.0, 20))
}

impl AdmissionMetrics {
    /// Registers observation families; only configuration and real events create model series.
    fn new() -> Self {
        let mut metrics = Self {
            registry: Registry::default(),
            catalog: Mutex::new(BTreeMap::new()),
            model_info: Family::default(),
            configured_models: Gauge::default(),
            enabled: Family::default(),
            waiting_limit: Family::default(),
            waiting: Family::default(),
            active: Family::default(),
            store_available: Family::default(),
            attempts: Family::default(),
            enqueued: Family::default(),
            results: Family::default(),
            wait: Family::new_with_constructor(wait_histogram as fn() -> Histogram),
        };
        metrics.registry.register(
            "foretoken_admission_model_info",
            "Models in the currently published serving catalog",
            metrics.model_info.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_configured_models",
            "Models in the currently published serving catalog",
            metrics.configured_models.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_enabled",
            "Whether bounded admission is configured for this model",
            metrics.enabled.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_shared_waiting_limit_work_units",
            "Configured service/model waiting limit; duplicate frontend observations must not be summed",
            metrics.waiting_limit.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_shared_waiting_work_units",
            "Ledger waiting units, including preparation and unresolved submission; deduplicate by service UID and model",
            metrics.waiting.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_shared_active_work_units",
            "Ledger dispatch-reserved plus accepted unfinished units; deduplicate by service UID and model",
            metrics.active.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_store_available",
            "Whether this frontend's latest service/model ledger observation succeeded",
            metrics.store_available.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_attempts",
            "Request admission calls started, not candidate units",
            metrics.attempts.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_enqueued",
            "Requests that acquired waiting ownership, not candidate units",
            metrics.enqueued.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_dispatch_results",
            "Requests ending admission at first backend acceptance, CPU preparation completion or terminal failure",
            metrics.results.clone(),
        );
        metrics.registry.register(
            "foretoken_admission_wait_seconds",
            "Time holding waiting ownership through backend acceptance, CPU preparation completion or terminal failure",
            metrics.wait.clone(),
        );
        metrics
    }

    fn remove_shared(&self, labels: &SharedLabels) {
        self.waiting_limit.remove(labels);
        self.waiting.remove(labels);
        self.active.remove(labels);
        self.store_available.remove(labels);
    }

    /// Retires all series for a removed catalog model, including cumulative request observations.
    fn remove_model(&self, model: &str, shared: Option<&SharedLabels>) {
        let labels = ModelLabels {
            model_name: model.into(),
        };
        self.model_info.remove(&labels);
        self.enabled.remove(&labels);
        if let Some(shared) = shared {
            self.remove_shared(shared);
        }
        for origin in ["http", "internal"] {
            let labels = CallLabels {
                model_name: model.into(),
                origin,
            };
            self.attempts.remove(&labels);
            self.enqueued.remove(&labels);
            for result in RESULTS {
                let labels = ResultLabels {
                    model_name: model.into(),
                    origin,
                    result,
                };
                self.results.remove(&labels);
                self.wait.remove(&labels);
            }
        }
    }
}

/// Publishes the committed model catalog and retires removed models before late events can arrive.
pub(crate) fn set_configured_models(models: &[String]) {
    let metrics = &METRICS;
    let mut catalog = metrics
        .catalog
        .lock()
        .expect("admission catalog lock poisoned");
    catalog.retain(|model, shared| {
        if models.contains(model) {
            true
        } else {
            metrics.remove_model(model, shared.as_ref());
            false
        }
    });
    for model in models {
        catalog.entry(model.clone()).or_default();
        metrics
            .model_info
            .get_or_create(&ModelLabels {
                model_name: model.clone(),
            })
            .set(1);
    }
    metrics.configured_models.set(catalog.len() as i64);
}

/// Publishes effective settings after catalog activation without resetting cumulative events.
/// Caller limits are not model concurrency limits and are not exported as a total quota.
pub(crate) fn set_model_config(model: &str, frontend_service_uid: &str, config: &AdmissionConfig) {
    let metrics = &METRICS;
    let mut catalog = metrics
        .catalog
        .lock()
        .expect("admission catalog lock poisoned");
    let Some(shared) = catalog.get_mut(model) else {
        return;
    };
    let labels = SharedLabels {
        model_name: model.into(),
        frontend_service_uid: frontend_service_uid.into(),
    };
    if let Some(previous) = shared.as_ref().filter(|previous| **previous != labels) {
        metrics.remove_shared(previous);
    }
    *shared = Some(labels.clone());
    metrics
        .enabled
        .get_or_create(&ModelLabels {
            model_name: model.into(),
        })
        .set(i64::from(config.max_waiting_requests.is_some()));
    if let Some(limit) = config.max_waiting_requests {
        metrics
            .waiting_limit
            .get_or_create(&labels)
            .set(i64::from(limit));
    } else {
        metrics.waiting_limit.remove(&labels);
    }
}

/// Publishes periodic ledger counts (waiting, reserved plus accepted unfinished units).
/// A failed read removes stale occupancy, but never changes admission or accepted ownership.
pub(crate) fn observe_ledger(
    model: &str,
    frontend_service_uid: &str,
    occupancy: Option<(u64, u64)>,
) {
    let metrics = &METRICS;
    let catalog = metrics
        .catalog
        .lock()
        .expect("admission catalog lock poisoned");
    let Some(Some(labels)) = catalog.get(model) else {
        return;
    };
    if labels.frontend_service_uid != frontend_service_uid {
        return;
    }
    metrics
        .store_available
        .get_or_create(labels)
        .set(i64::from(occupancy.is_some()));
    if let Some((waiting, active)) = occupancy {
        metrics.waiting.get_or_create(labels).set(waiting as i64);
        metrics.active.get_or_create(labels).set(active as i64);
    } else {
        metrics.waiting.remove(labels);
        metrics.active.remove(labels);
    }
}

/// Shares one request's terminal observation between its caller and resource owner.
/// Callers retain a clone before transferring permits to non-cancelable preparation;
/// recording a result does not release resources or wait for their cleanup.
#[derive(Clone)]
pub struct AdmissionAttempt(Arc<Mutex<AttemptState>>);

struct AttemptState {
    labels: CallLabels,
    deadline: tokio::time::Instant,
    deadline_elapsed: Option<Arc<AtomicBool>>,
    waiting_since: Option<Instant>,
    finished: bool,
}

impl AdmissionAttempt {
    /// Starts admission before validation or enqueue; HTTP scope determines the call origin.
    pub fn work(model: &str, deadline: tokio::time::Instant) -> Self {
        let deadline_elapsed = HTTP_OBSERVATION
            .try_with(|scope| scope.deadline_elapsed.clone())
            .ok();
        let labels = CallLabels {
            model_name: model.into(),
            origin: if deadline_elapsed.is_some() {
                "http"
            } else {
                "internal"
            },
        };
        let catalog = METRICS
            .catalog
            .lock()
            .expect("admission catalog lock poisoned");
        if catalog.contains_key(model) {
            METRICS.attempts.get_or_create(&labels).inc();
        }
        Self(Arc::new(Mutex::new(AttemptState {
            labels,
            deadline,
            deadline_elapsed,
            waiting_since: None,
            finished: false,
        })))
    }

    /// Records successful enqueue once, after waiting ownership has been acquired.
    pub fn begin_wait(&self) {
        let mut state = self.0.lock().expect("admission attempt lock poisoned");
        if state.finished || state.waiting_since.is_some() {
            return;
        }
        state.waiting_since = Some(Instant::now());
        let catalog = METRICS
            .catalog
            .lock()
            .expect("admission catalog lock poisoned");
        if catalog.contains_key(&state.labels.model_name) {
            METRICS.enqueued.get_or_create(&state.labels).inc();
        }
    }

    /// Records first backend acceptance or a terminal admission error, whichever finishes first.
    pub fn complete<T>(&self, result: &Result<T, AdmissionError>) {
        self.0
            .lock()
            .expect("admission attempt lock poisoned")
            .record(match result {
                Ok(_) => "dispatched",
                Err(AdmissionError::Overloaded) => "capacity_rejected",
                Err(AdmissionError::QueueTimeout) => "queue_timeout",
                Err(AdmissionError::DeadlineExceeded) => "deadline_exceeded",
                Err(AdmissionError::BatchTooLarge | AdmissionError::IdentityRequired) => {
                    "invalid_request"
                }
                Err(AdmissionError::Closed) => "closed",
                Err(AdmissionError::StoreUnavailable) => "store_unavailable",
            });
    }

    /// Records frontend input validation failure without assigning an admission identity cause.
    /// Protocol preparation calls this before handing work to a backend; it releases no capacity.
    pub fn invalid_request(&self) {
        self.0
            .lock()
            .expect("admission attempt lock poisoned")
            .record("invalid_request");
    }

    /// Records successful CPU-only preparation without claiming backend acceptance.
    pub fn prepared(&self) {
        self.0
            .lock()
            .expect("admission attempt lock poisoned")
            .record("prepared");
    }

    /// Records a terminal execution-path error, not an explicit busy retry.
    pub fn dispatch_failed(&self) {
        self.0
            .lock()
            .expect("admission attempt lock poisoned")
            .record("dispatch_failed");
    }

    /// Records cancellation before asynchronous resource cleanup, preserving the HTTP cause.
    pub fn cancelled(&self) {
        self.0
            .lock()
            .expect("admission attempt lock poisoned")
            .cancelled();
    }
}

impl AttemptState {
    /// Uses the captured HTTP cancellation cause; only internal calls infer expiry from time.
    fn cancelled(&mut self) {
        let expired = self.deadline_elapsed.as_ref().map_or_else(
            || tokio::time::Instant::now() >= self.deadline,
            |flag| flag.load(Ordering::Relaxed),
        );
        self.record(if expired {
            "deadline_exceeded"
        } else {
            "cancelled"
        });
    }

    /// Serializes terminal outcomes and suppresses late events for retired catalog models.
    fn record(&mut self, result: &'static str) {
        if self.finished {
            return;
        }
        self.finished = true;
        let metrics = &METRICS;
        let catalog = metrics
            .catalog
            .lock()
            .expect("admission catalog lock poisoned");
        if catalog.contains_key(&self.labels.model_name) {
            let labels = ResultLabels {
                model_name: self.labels.model_name.clone(),
                origin: self.labels.origin,
                result,
            };
            metrics.results.get_or_create(&labels).inc();
            if let Some(started) = self.waiting_since {
                metrics
                    .wait
                    .get_or_create(&labels)
                    .observe(started.elapsed().as_secs_f64());
            }
        }
    }
}

impl Drop for AttemptState {
    fn drop(&mut self) {
        // Only the final handle falls back. Delayed CPU or ledger cleanup must not turn an
        // HTTP disconnect into a deadline; explicit caller results take precedence over cleanup.
        if !self.finished {
            self.cancelled();
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

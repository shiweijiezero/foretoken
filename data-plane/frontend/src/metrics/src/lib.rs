// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM-compatible Prometheus metrics and Foretoken-owned admission telemetry.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use axum::extract::{MatchedPath, Request};
use axum::http::header::CONTENT_TYPE;
use axum::http::{HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use foretoken_router::{RouteTargetSet, ScalingTarget};
use prometheus_client::collector::Collector;
use prometheus_client::encoding::text::{encode_eof, encode_registry};
use prometheus_client::encoding::{DescriptorEncoder, EncodeLabelSet};
use prometheus_client::metrics::MetricType;
use prometheus_client::registry::Registry;
use serde::Serialize;

pub use vllm_metrics::*;

const OPENMETRICS_CONTENT_TYPE: &str = "application/openmetrics-text; version=1.0.0; charset=utf-8";
/// Routes served to the platform rather than to inference clients. They are not counted as
/// requests and receive no request span, so scrapes and probes do not appear in request logs.
pub const EXCLUDED_HANDLERS: &[&str] = &[
    "/metrics",
    "/healthz",
    "/health",
    "/readyz",
    "/statusz",
    "/internal/autoscaling/telemetry",
];

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, EncodeLabelSet)]
pub struct QueuedTarget {
    pub service_uid: String,
    pub target_kind: String,
    pub target_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AutoscalingTargetTelemetry {
    #[serde(flatten)]
    pub target: QueuedTarget,
    pub runtime_queued_requests: u64,
    pub dispatch_queued_requests: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AutoscalingTelemetry {
    pub version: u8,
    pub collected_at_unix_ms: u64,
    pub targets: Vec<AutoscalingTargetTelemetry>,
}

#[derive(Debug, Clone, Copy)]
enum QueueStage {
    RuntimePreparation,
    BackendDispatch,
}

#[derive(Debug, Clone, Copy, Default)]
struct QueueCounts {
    runtime_preparation: u64,
    backend_dispatch: u64,
    runtime_scopes: usize,
    observed: bool,
}

#[derive(Debug)]
struct FrontendCollector {
    kv_index: (String, Option<String>, usize, usize),
}

static QUEUED: OnceLock<Mutex<BTreeMap<QueuedTarget, QueueCounts>>> = OnceLock::new();

/// Retains observed zero-valued queue labels while an immutable runtime can still use their Pools.
pub struct QueueMetricsScope {
    targets: Vec<QueuedTarget>,
}

/// RAII ownership of one request waiting for admission to a fixed target set.
pub struct QueueGuard {
    targets: Vec<QueuedTarget>,
    stage: QueueStage,
}
impl From<&ScalingTarget> for QueuedTarget {
    fn from(target: &ScalingTarget) -> Self {
        Self {
            service_uid: target.service_uid.clone(),
            target_kind: "Pool".to_owned(),
            target_id: target.uid.clone(),
        }
    }
}

impl QueueMetricsScope {
    /// Registers one runtime's candidate Pools without publishing unobserved idle samples.
    /// Runtime state retains the scope until its last request releases that generation.
    pub fn new(candidates: &[RouteTargetSet]) -> Self {
        let targets = candidates
            .iter()
            .flat_map(|candidate| candidate.targets())
            .map(QueuedTarget::from)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>();
        let mut values = queued_lock();
        for target in &targets {
            values.entry(target.clone()).or_default().runtime_scopes += 1;
        }
        Self { targets }
    }
}

impl Drop for QueueMetricsScope {
    fn drop(&mut self) {
        let mut values = queued_lock();
        for target in &self.targets {
            let counts = values
                .get_mut(target)
                .expect("queue metric scope is registered");
            counts.runtime_scopes -= 1;
            if counts.runtime_scopes == 0
                && counts.runtime_preparation == 0
                && counts.backend_dispatch == 0
            {
                values.remove(target);
            }
        }
    }
}

impl QueueGuard {
    /// Attributes a request waiting for its model runtime to become ready.
    pub fn runtime_preparation(targets: &RouteTargetSet) -> Self {
        Self::new(targets, QueueStage::RuntimePreparation)
    }

    /// Attributes a routed request waiting for the backend stream to begin.
    pub fn backend_dispatch(targets: &RouteTargetSet) -> Self {
        Self::new(targets, QueueStage::BackendDispatch)
    }

    fn new(targets: &RouteTargetSet, stage: QueueStage) -> Self {
        let targets = targets
            .targets()
            .iter()
            .map(QueuedTarget::from)
            .collect::<Vec<_>>();
        let mut values = queued_lock();
        for target in &targets {
            let counts = values.entry(target.clone()).or_default();
            counts.observed = true;
            match stage {
                QueueStage::RuntimePreparation => counts.runtime_preparation += 1,
                QueueStage::BackendDispatch => counts.backend_dispatch += 1,
            }
        }
        drop(values);
        Self { targets, stage }
    }
}
impl Drop for QueueGuard {
    fn drop(&mut self) {
        let mut values = queued_lock();
        for target in &self.targets {
            let counts = values
                .get_mut(target)
                .expect("queued request target is registered");
            let value = match self.stage {
                QueueStage::RuntimePreparation => &mut counts.runtime_preparation,
                QueueStage::BackendDispatch => &mut counts.backend_dispatch,
            };
            *value = value.saturating_sub(1);
            if counts.runtime_scopes == 0
                && counts.runtime_preparation == 0
                && counts.backend_dispatch == 0
            {
                values.remove(target);
            }
        }
    }
}

/// Snapshots frontend-owned admission queue counts for autoscaling consumers.
///
/// The telemetry endpoint serializes the returned value; it is a derived report and does not retain a lock or request ownership.
pub fn autoscaling_telemetry() -> AutoscalingTelemetry {
    let targets = queued_lock()
        .iter()
        .filter(|(_, counts)| counts.observed)
        .map(|(target, counts)| AutoscalingTargetTelemetry {
            target: target.clone(),
            runtime_queued_requests: counts.runtime_preparation,
            dispatch_queued_requests: counts.backend_dispatch,
        })
        .collect();
    let collected_at_unix_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX);
    AutoscalingTelemetry {
        version: 2,
        collected_at_unix_ms,
        targets,
    }
}

/// Renders the OpenMetrics response with a caller-provided KV-index status snapshot.
///
/// The KV-aware metrics route supplies the status values; their ownership remains with the indexer.
pub async fn scrape_with_kv_index(
    state: &str,
    reason: Option<&str>,
    sources_healthy: usize,
    sources_total: usize,
) -> Response {
    render((state, reason, sources_healthy, sources_total))
}

/// Records one non-metrics HTTP response start after its handler returns a response.
///
/// For SSE endpoints, Axum returns before the body stream finishes, so the duration histogram
/// measures response-start latency. Complete generation latency remains owned by the model-server
/// request metrics and is exposed through `vllm:e2e_request_latency_seconds`.
pub async fn track_http_metrics(request: Request, next: Next) -> Response {
    let method = request.method().as_str().to_owned();
    let handler = request
        .extensions()
        .get::<MatchedPath>()
        .map_or_else(|| "none".to_owned(), |path| path.as_str().to_owned());
    let excluded = EXCLUDED_HANDLERS.contains(&handler.as_str());
    let started_at = Instant::now();
    let response = next.run(request).await;
    if excluded {
        return response;
    }
    let response_start_latency = started_at.elapsed().as_secs_f64();
    let metrics = &METRICS.api_server;
    metrics
        .http_requests
        .get_or_create(&HttpRequestLabels {
            method: method.clone(),
            status: status_group(response.status().as_u16()),
            handler: handler.clone(),
        })
        .inc();
    metrics
        .http_request_duration_seconds
        .get_or_create(&HttpHandlerLabels { method, handler })
        .observe(response_start_latency);
    metrics
        .http_request_duration_highr_seconds
        .observe(response_start_latency);
    response
}

fn queued() -> &'static Mutex<BTreeMap<QueuedTarget, QueueCounts>> {
    QUEUED.get_or_init(|| Mutex::new(BTreeMap::new()))
}
fn queued_lock() -> MutexGuard<'static, BTreeMap<QueuedTarget, QueueCounts>> {
    queued()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

// All owners encode into one OpenMetrics document; only the endpoint writes its EOF marker.
fn render((state, reason, healthy, total): (&str, Option<&str>, usize, usize)) -> Response {
    let encoded = (|| {
        let mut body = String::new();
        encode_registry(&mut body, METRICS.registry())?;
        foretoken_router::encode_metrics(&mut body)?;
        foretoken_admission::encode_metrics(&mut body)?;
        let mut registry = Registry::default();
        registry.register_collector(Box::new(FrontendCollector {
            kv_index: (state.to_owned(), reason.map(str::to_owned), healthy, total),
        }));
        encode_registry(&mut body, &registry)?;
        encode_eof(&mut body)?;
        Ok::<_, fmt::Error>(body)
    })();
    match encoded {
        Ok(body) => (
            [(
                CONTENT_TYPE,
                HeaderValue::from_static(OPENMETRICS_CONTENT_TYPE),
            )],
            body,
        )
            .into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

impl Collector for FrontendCollector {
    /// Encodes owner-held queues and the scrape's KV snapshot without mirrored gauge state.
    fn encode(&self, mut encoder: DescriptorEncoder<'_>) -> Result<(), fmt::Error> {
        let mut queues = encoder.encode_descriptor(
            "foretoken_upstream_queued_requests",
            "Requests waiting for admission to a scaling target",
            None,
            MetricType::Gauge,
        )?;
        for (target, counts) in queued_lock().iter().filter(|(_, counts)| counts.observed) {
            let count = counts
                .runtime_preparation
                .saturating_add(counts.backend_dispatch);
            queues.encode_family(target)?.encode_gauge(&count)?;
        }
        let (state, reason, healthy, total) = &self.kv_index;
        for (name, help, value) in [
            (
                "foretoken_kv_index_enabled",
                "Whether KV locality indexing is enabled",
                u64::from(!matches!(state.as_str(), "disabled" | "unavailable")),
            ),
            (
                "foretoken_kv_index_sources_healthy",
                "Healthy KV event sources",
                *healthy as u64,
            ),
            (
                "foretoken_kv_index_sources_total",
                "Configured KV event sources",
                *total as u64,
            ),
        ] {
            encoder
                .encode_descriptor(name, help, None, MetricType::Gauge)?
                .encode_gauge(&value)?;
        }
        encoder
            .encode_descriptor(
                "foretoken_kv_index_degraded",
                "Whether KV locality observations are degraded",
                None,
                MetricType::Gauge,
            )?
            .encode_family(&[("reason", reason.as_deref().unwrap_or("none"))])?
            .encode_gauge(&u64::from(state == "degraded"))?;
        Ok(())
    }
}

fn status_group(status: u16) -> &'static str {
    match status / 100 {
        1 => "1xx",
        2 => "2xx",
        3 => "3xx",
        4 => "4xx",
        5 => "5xx",
        _ => "unknown",
    }
}

// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM-compatible Prometheus metrics and Foretoken-owned admission telemetry.

use std::collections::BTreeMap;
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use axum::extract::{MatchedPath, Request};
use axum::http::header::CONTENT_TYPE;
use axum::http::{HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use foretoken_router::{RouteTargetSet, ScalingTarget, ScalingTargetKind};
use serde::Serialize;

pub use vllm_metrics::*;

const OPENMETRICS_CONTENT_TYPE: &str = "application/openmetrics-text; version=1.0.0; charset=utf-8";
const EXCLUDED_HANDLERS: &[&str] = &[
    "/metrics",
    "/healthz",
    "/readyz",
    "/statusz",
    "/internal/autoscaling/telemetry",
];

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub struct QueuedTarget {
    pub service_uid: String,
    pub target_kind: String,
    pub target_id: String,
}

impl From<&ScalingTarget> for QueuedTarget {
    fn from(target: &ScalingTarget) -> Self {
        Self {
            service_uid: target.service_uid.clone(),
            target_kind: match target.kind {
                ScalingTargetKind::Pool => "Pool",
                ScalingTargetKind::EPDPipelineScope => "EPDPipelineScope",
            }
            .to_owned(),
            target_id: if target.kind == ScalingTargetKind::Pool {
                target.uid.clone()
            } else {
                target.service_uid.clone()
            },
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AutoscalingTargetTelemetry {
    #[serde(flatten)]
    pub target: QueuedTarget,
    pub queued_requests: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AutoscalingTelemetry {
    pub version: u8,
    pub collected_at_unix_ms: u64,
    pub targets: Vec<AutoscalingTargetTelemetry>,
}

static QUEUED: OnceLock<Mutex<BTreeMap<QueuedTarget, u64>>> = OnceLock::new();
fn queued() -> &'static Mutex<BTreeMap<QueuedTarget, u64>> {
    QUEUED.get_or_init(|| Mutex::new(BTreeMap::new()))
}
fn queued_lock() -> MutexGuard<'static, BTreeMap<QueuedTarget, u64>> {
    queued()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// RAII ownership of one request waiting for admission to a fixed target set.
pub struct QueueGuard {
    targets: Vec<QueuedTarget>,
}
impl QueueGuard {
    /// Begins queue attribution for a request waiting on every required scaling target.
    ///
    /// The admission path owns the guard while the request remains queued; dropping it removes the request from telemetry.
    pub fn new(targets: &RouteTargetSet) -> Self {
        let targets = targets
            .targets()
            .iter()
            .map(QueuedTarget::from)
            .collect::<Vec<_>>();
        let mut values = queued_lock();
        for target in &targets {
            *values.entry(target.clone()).or_default() += 1;
        }
        drop(values);
        Self { targets }
    }
}
impl Drop for QueueGuard {
    fn drop(&mut self) {
        let mut values = queued_lock();
        for target in &self.targets {
            let value = values.entry(target.clone()).or_default();
            *value = value.saturating_sub(1);
        }
    }
}

/// Snapshots frontend-owned admission queue counts for autoscaling consumers.
///
/// The telemetry endpoint serializes the returned value; it is a derived report and does not retain a lock or request ownership.
pub fn autoscaling_telemetry() -> AutoscalingTelemetry {
    let targets = queued_lock()
        .iter()
        .map(|(target, queued_requests)| AutoscalingTargetTelemetry {
            target: target.clone(),
            queued_requests: *queued_requests,
        })
        .collect();
    let collected_at_unix_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX);
    AutoscalingTelemetry {
        version: 1,
        collected_at_unix_ms,
        targets,
    }
}

/// Renders the frontend's OpenMetrics response without KV-index status for callers that do not expose index diagnostics.
pub async fn scrape() -> Response {
    render(None)
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
    render(Some((state, reason, sources_healthy, sources_total)))
}

// Renders upstream metrics first, then appends Foretoken-owned admission and optional KV-index
// families before restoring the single OpenMetrics EOF marker.
fn render(kv_index: Option<(&str, Option<&str>, usize, usize)>) -> Response {
    match METRICS.render() {
        Ok(mut body) => {
            if let Some(without_eof) = body.strip_suffix("# EOF\n") {
                body = without_eof.to_owned();
            }
            body.push_str(&render_admission_metrics());
            if let Some((state, reason, sources_healthy, sources_total)) = kv_index {
                body.push_str(&format!("# TYPE foretoken_kv_index_enabled gauge\nforetoken_kv_index_enabled {}\n# TYPE foretoken_kv_index_degraded gauge\nforetoken_kv_index_degraded{{reason=\"{}\"}} {}\n# TYPE foretoken_kv_index_sources_healthy gauge\nforetoken_kv_index_sources_healthy {}\n# TYPE foretoken_kv_index_sources_total gauge\nforetoken_kv_index_sources_total {}\n", usize::from(!matches!(state, "disabled" | "unavailable")), escape_label(reason.unwrap_or("none")), usize::from(state == "degraded"), sources_healthy, sources_total));
            }
            body.push_str("# EOF\n");
            (
                [(
                    CONTENT_TYPE,
                    HeaderValue::from_static(OPENMETRICS_CONTENT_TYPE),
                )],
                body,
            )
                .into_response()
        }
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

fn render_admission_metrics() -> String {
    let values = queued_lock();
    let mut body = String::from(
        "# TYPE foretoken_upstream_queued_requests gauge\n# HELP foretoken_upstream_queued_requests Requests waiting for admission to a scaling target.\n",
    );
    for (target, value) in values.iter() {
        body.push_str(&format!("foretoken_upstream_queued_requests{{service_uid=\"{}\",target_kind=\"{}\",target_id=\"{}\"}} {}\n", escape_label(&target.service_uid), escape_label(&target.target_kind), escape_label(&target.target_id), value));
    }
    body
}
fn escape_label(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('\n', "\\n")
        .replace('"', "\\\"")
}

/// Records one non-metrics HTTP request after its handler completes.
///
/// The Axum middleware stack consumes the unchanged response while this function updates frontend-owned vLLM-compatible counters.
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
    let elapsed = started_at.elapsed().as_secs_f64();
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
        .observe(elapsed);
    metrics.http_request_duration_highr_seconds.observe(elapsed);
    response
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

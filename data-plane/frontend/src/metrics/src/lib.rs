// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM-compatible Prometheus metrics and Axum adapters for Foretoken.

use std::time::Instant;

use axum::extract::{MatchedPath, Request};
use axum::http::header::CONTENT_TYPE;
use axum::http::{HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};

pub use vllm_metrics::*;

const OPENMETRICS_CONTENT_TYPE: &str = "application/openmetrics-text; version=1.0.0; charset=utf-8";
const EXCLUDED_HANDLERS: &[&str] = &["/metrics", "/healthz", "/readyz", "/statusz"];

pub async fn scrape() -> Response {
    render(None)
}

pub async fn scrape_with_kv_index(
    state: &str,
    reason: Option<&str>,
    sources_healthy: usize,
    sources_total: usize,
) -> Response {
    render(Some((state, reason, sources_healthy, sources_total)))
}

fn render(kv_index: Option<(&str, Option<&str>, usize, usize)>) -> Response {
    match METRICS.render() {
        Ok(mut body) => {
            if let Some((state, reason, sources_healthy, sources_total)) = kv_index {
                let suffix = format!(
                    "# TYPE foretoken_kv_index_enabled gauge\nforetoken_kv_index_enabled {}\n# TYPE foretoken_kv_index_degraded gauge\nforetoken_kv_index_degraded{{reason=\"{}\"}} {}\n# TYPE foretoken_kv_index_sources_healthy gauge\nforetoken_kv_index_sources_healthy {}\n# TYPE foretoken_kv_index_sources_total gauge\nforetoken_kv_index_sources_total {}\n",
                    usize::from(!matches!(state, "disabled" | "unavailable")),
                    reason.unwrap_or("none"),
                    usize::from(state == "degraded"),
                    sources_healthy,
                    sources_total,
                );
                if let Some(without_eof) = body.strip_suffix("# EOF\n") {
                    body = format!("{without_eof}{suffix}# EOF\n");
                } else {
                    body.push_str(&suffix);
                }
            }
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

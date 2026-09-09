// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Process-local JSON logging, OTLP export, and request context propagation.

use std::collections::BTreeMap;
use std::env;
use std::pin::Pin;
use std::task::{Context, Poll};

use http_body::{Body, Frame, SizeHint};
use opentelemetry::propagation::{Extractor, Injector, TextMapPropagator};
use opentelemetry::trace::{TraceContextExt, TracerProvider};
use opentelemetry_otlp::WithExportConfig;
use opentelemetry_sdk::Resource;
use opentelemetry_sdk::propagation::TraceContextPropagator;
use opentelemetry_sdk::trace::{Sampler, SdkTracerProvider};
use tracing::Span;
pub use tracing_opentelemetry::OpenTelemetrySpanExt;
use tracing_subscriber::EnvFilter;
use tracing_subscriber::prelude::*;

/// W3C parent header propagated between inference processes.
pub const TRACEPARENT_HEADER: &str = "traceparent";
/// W3C vendor state propagated with a valid parent context.
pub const TRACESTATE_HEADER: &str = "tracestate";
/// Server-owned identity returned to clients for log correlation.
pub const REQUEST_ID_HEADER: &str = "x-request-id";

/// HTTP-owned identity and arrival time shared by all candidates of one request.
#[derive(Clone, Debug)]
pub struct RequestContext {
    pub request_id: String,
    pub arrival_time: f64,
    pub trace_headers: Option<BTreeMap<String, String>>,
}

/// Owns the process tracer provider until all serving tasks have stopped.
///
/// Executable entry points retain this guard outside the Tokio runtime so its final
/// batch flush can use the blocking HTTP exporter after asynchronous tasks drain.
pub struct TracingGuard {
    provider: SdkTracerProvider,
}

impl Drop for TracingGuard {
    fn drop(&mut self) {
        if let Err(error) = self.provider.shutdown() {
            tracing::warn!(%error, "trace exporter shutdown failed");
        }
    }
}

/// Installs JSON logs and optional HTTP/protobuf OTLP export for a serving executable.
///
/// Call once before starting Tokio and retain the guard through shutdown. An explicit
/// OTLP endpoint enables export; SDK environment variables own credentials and sampling.
pub fn init_tracing(
    service_name: &'static str,
) -> Result<TracingGuard, Box<dyn std::error::Error + Send + Sync>> {
    let endpoint = otlp_traces_endpoint();
    // Environment resource attributes override the process defaults, including service.name.
    let resource = Resource::builder_empty()
        .with_service_name(service_name)
        .with_detector(Box::new(
            opentelemetry_sdk::resource::EnvResourceDetector::new(),
        ))
        .build();
    let service = env::var("OTEL_SERVICE_NAME").ok();
    let resource = if let Some(service) = service {
        Resource::builder_empty()
            .with_attributes(
                resource
                    .iter()
                    .map(|(key, value)| opentelemetry::KeyValue::new(key.clone(), value.clone())),
            )
            .with_service_name(service)
            .build()
    } else {
        resource
    };
    let mut builder = SdkTracerProvider::builder().with_resource(resource);
    if let Some(endpoint) = endpoint {
        let protocol = env::var("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
            .or_else(|_| env::var("OTEL_EXPORTER_OTLP_PROTOCOL"))
            .unwrap_or_else(|_| "http/protobuf".into());
        if protocol != "http/protobuf" {
            return Err(
                std::io::Error::other("Foretoken tracing requires OTLP http/protobuf").into(),
            );
        }
        let exporter = opentelemetry_otlp::SpanExporter::builder()
            .with_http()
            .with_endpoint(endpoint)
            .build()?;
        builder = builder.with_batch_exporter(exporter);
    } else {
        // Keep request identities and context propagation without storing or exporting spans.
        builder = builder.with_sampler(Sampler::AlwaysOff);
    }
    let provider = builder.build();
    let tracer = provider.tracer("foretoken");
    let filter = EnvFilter::builder()
        .with_default_directive(tracing::level_filters::LevelFilter::INFO.into())
        .from_env()?;
    tracing_subscriber::registry()
        .with(filter)
        .with(tracing_opentelemetry::layer().with_tracer(tracer))
        .with(
            tracing_subscriber::fmt::layer()
                .json()
                .with_current_span(true)
                .with_span_list(true),
        )
        .try_init()?;
    Ok(TracingGuard { provider })
}

/// Resolves the shared HTTP trace destination for the Rust exporter and engine adapter.
///
/// A signal-specific endpoint is already complete; the generic endpoint is a base URL.
/// Without an endpoint, or when export is explicitly disabled, neither process exports.
pub fn otlp_traces_endpoint() -> Option<String> {
    if env::var("OTEL_TRACES_EXPORTER").as_deref() == Ok("none")
        || env::var("OTEL_SDK_DISABLED").as_deref() == Ok("true")
    {
        return None;
    }
    env::var("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        .ok()
        .filter(|value| !value.is_empty())
        .or_else(|| {
            env::var("OTEL_EXPORTER_OTLP_ENDPOINT")
                .ok()
                .filter(|value| !value.is_empty())
                .map(|base| format!("{}/v1/traces", base.trim_end_matches('/')))
        })
}

struct Headers<'a>(&'a BTreeMap<String, String>);
impl Extractor for Headers<'_> {
    fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).map(String::as_str)
    }

    fn keys(&self) -> Vec<&str> {
        self.0.keys().map(String::as_str).collect()
    }
}

struct HeaderValues(BTreeMap<String, String>);
impl Injector for HeaderValues {
    fn set(&mut self, key: &str, value: String) {
        self.0.insert(key.to_owned(), value);
    }
}

/// Extracts an incoming W3C parent using the SDK and binds it before span activity begins.
///
/// Callers declare empty trace_id/span_id fields; this records SDK-assigned IDs for JSON logs.
pub fn set_parent(span: &Span, headers: Option<&BTreeMap<String, String>>) {
    if let Some(headers) = headers {
        let parent = TraceContextPropagator::new().extract(&Headers(headers));
        if parent.span().span_context().is_valid() {
            // Library users may intentionally run without a subscriber; no layer means no trace.
            let _ = span.set_parent(parent);
        }
    }
    record_context(span);
}

/// Records an already-created span's SDK identities in its declared JSON log fields.
pub fn record_context(span: &Span) {
    let context = span.context();
    let span_context = context.span().span_context().clone();
    if span_context.is_valid() {
        span.record("trace_id", span_context.trace_id().to_string());
        span.record("span_id", span_context.span_id().to_string());
    }
}

/// Marks a request span failed using a stable diagnostic without request content.
pub fn mark_error(span: &Span, message: &'static str) {
    span.set_status(opentelemetry::trace::Status::error(message));
    tracing::warn!(parent: span, reason = message, "request failed");
}

/// Serializes a span's W3C context for the next transport or EngineCore request.
pub fn propagation_headers(span: &Span) -> Option<BTreeMap<String, String>> {
    let mut headers = HeaderValues(BTreeMap::new());
    TraceContextPropagator::new().inject_context(&span.context(), &mut headers);
    (!headers.0.is_empty()).then_some(headers.0)
}

/// Retains and enters a request span while polling an HTTP body, including its trailers.
///
/// The response owns this wrapper until EOF, error, or disconnect. Closing it emits one
/// terminal log; an error marks the span failed, while disconnect is reported as cancellation.
pub struct TracedBody<B: Body> {
    body: Pin<Box<B>>,
    span: Option<Span>,
}

impl<B: Body> TracedBody<B> {
    /// Transfers a response body and its live request span to the HTTP transport.
    pub fn new(body: B, span: Span) -> Self {
        let mut traced = Self {
            body: Box::pin(body),
            span: Some(span),
        };
        if traced.body.is_end_stream() {
            traced.finish("completed");
        }
        traced
    }

    fn finish(&mut self, outcome: &'static str) {
        if let Some(span) = self.span.take() {
            if outcome == "error" {
                span.set_status(opentelemetry::trace::Status::error(
                    "response stream failed",
                ));
            }
            tracing::info!(parent: &span, outcome, "request finished");
        }
    }
}

impl<B: Body> Body for TracedBody<B> {
    type Data = B::Data;
    type Error = B::Error;

    fn poll_frame(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
        let this = self.get_mut();
        let span = this.span.clone().unwrap_or_else(Span::none);
        let result = {
            let _entered = span.enter();
            this.body.as_mut().poll_frame(cx)
        };
        match &result {
            Poll::Ready(None) => this.finish("completed"),
            Poll::Ready(Some(Err(_))) => this.finish("error"),
            Poll::Ready(Some(Ok(_))) if this.body.is_end_stream() => this.finish("completed"),
            _ => {}
        }
        result
    }

    fn is_end_stream(&self) -> bool {
        self.body.is_end_stream()
    }

    fn size_hint(&self) -> SizeHint {
        self.body.size_hint()
    }
}

impl<B: Body> Drop for TracedBody<B> {
    fn drop(&mut self) {
        self.finish("cancelled");
    }
}

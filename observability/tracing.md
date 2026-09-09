<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Distributed tracing

[Observability](README.md) | [简体中文](tracing_zh.md)

Foretoken's frontend and model-server accept W3C `traceparent`/`tracestate`, emit JSON request logs with `request_id`, `trace_id`, and `span_id`, and export sampled spans through OTLP/HTTP when configured. Tracing is disabled unless an endpoint is provided.

## Configure the platform

Use the maintained platform values example rather than editing the Chart directly:

```bash
foretoken install --values examples/tracing/platform-values.yaml
```

Set these values for a real collector:

```yaml
observability:
  tracing:
    endpoint: http://otel-collector.observability.svc:4318
    samplingRatio: 0.1
    # headersSecret:
    #   name: otel-exporter-headers
    #   key: headers
```

`endpoint` is an OTLP/HTTP base URL; Foretoken uses the `/v1/traces` trace path. `samplingRatio` is the parent-based trace sampling probability. An optional `headersSecret` supplies the complete OTLP headers value through a Kubernetes Secret; keep credentials out of YAML and Git.

The platform passes the following environment to frontend and model-server Pods:

- `OTEL_EXPORTER_OTLP_ENDPOINT`;
- `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`;
- `OTEL_TRACES_SAMPLER=parentbased_traceidratio`;
- `OTEL_TRACES_SAMPLER_ARG=<samplingRatio>`;
- optional `OTEL_EXPORTER_OTLP_HEADERS` from the referenced Secret.

Set `OTEL_TRACES_EXPORTER=none` or omit the endpoint to disable export. Trace context propagation and request IDs remain safe to use without an exporter.

## Follow one request

A client may provide `traceparent`; Foretoken creates a server span and returns its server-owned `x-request-id`. The frontend propagates the W3C context to the model-server, which propagates it to the EngineCore runtime when the selected backend supports tracing. Logs contain only bounded identifiers and operation metadata; prompts, tokens, and credentials are not recorded.

A streaming span remains active until the response body completes, fails, or is cancelled. An SSE error event is recorded as an inference failure separately from an HTTP response that has already started; an HTTP `2xx` alone is not an inference-success signal.

## Collector requirements

Use an OpenTelemetry Collector or another OTLP/HTTP receiver at the configured endpoint. Configure retention, access control, batching, and export to the trace backend in that platform. Foretoken does not install a collector, tracing database, or log backend.

For notifications based on sustained metric conditions, see [Alerting](alerting.md).

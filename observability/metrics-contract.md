# Foretoken Metrics Contract

Status: MVP implementation draft

This document records Foretoken metric boundaries and the initial
implementation defaults. The contract can evolve through code review and
operational experience.

## Scope

The first observability milestone covers metrics produced by the Frontend,
model-server, vLLM engine, and control plane. Kubernetes and accelerator data
come from platform exporters.

Prometheus is an observability dependency, not part of the autoscaling control
loop. Autoscaling continues to use the existing versioned JSON telemetry APIs.
Dashboards, alerts, notification delivery, tracing, Torch Profiler, and Nsight
are separate follow-up changes.

## Measurement boundaries

The same latency name must not be reused across these boundaries:

| Boundary | Owner | Starts | Ends | Current status |
| --- | --- | --- | --- | --- |
| Client TTFT/TTFB | Benchmark or client | Client sends request | Client receives first response token or byte | Benchmark only |
| Frontend TTFT | Frontend | OpenAI-compatible request accepted | First non-empty token chunk yielded | Missing |
| Frontend stream E2E | Frontend | OpenAI-compatible request accepted | Response body completes, fails, or is dropped | Missing |
| Stage TTFT | model-server | Backend generation starts | First non-empty token produced | JSON telemetry |
| Stage TPOT | model-server | First token produced | Successful terminal output, divided by subsequent token count | JSON telemetry |
| Stage E2E | model-server | Backend generation starts | Successful terminal output produced | JSON telemetry |

Frontend metrics do not include downstream Gateway, network, proxy buffering,
or client processing time. Only a benchmark or client can measure a true
client-observed latency.

The existing Frontend HTTP duration stops when an HTTP response object is
returned. For streaming responses it is not stream E2E. Its status label is the
response-start HTTP status, so a later stream error can still appear as HTTP
2xx. It must not be used as an inference success or error SLO.

Foretoken stage TPOT is one average sample per eligible request:

```text
(successful_terminal_time - first_token_time) / (generated_tokens - 1)
```

It is not inter-token latency (ITL).

## Current producer inventory

| Signal | Owner | Type | Unit | Current availability |
| --- | --- | --- | --- | --- |
| HTTP requests by method, route, and response-start status class | Frontend | Counter | requests | OpenMetrics |
| HTTP handler duration | Frontend | Histogram | seconds | OpenMetrics; not stream E2E |
| Upstream queued requests | Frontend | Gauge | requests | OpenMetrics; not vLLM queue depth |
| Admission accepting | model-server | Boolean gauge candidate | 0 or 1 | JSON telemetry only |
| Model-server running requests | model-server | Gauge candidate | requests | JSON telemetry only |
| Maximum concurrent requests | model-server | Gauge candidate | requests | JSON telemetry only |
| Scheduler running and waiting requests | vLLM | Gauge | requests | Native registry and JSON snapshot |
| KV cache utilization | vLLM | Gauge | ratio from 0 to 1 | Native registry and JSON snapshot |
| Prompt and generation tokens | vLLM | Counter | tokens | Native registry and JSON snapshot |
| Stage TTFT, TPOT, and E2E | model-server | Cumulative histogram | seconds | Foretoken-owned JSON snapshot |
| Reconciliation and scaling decisions | Control plane | Counter/Histogram/Gauge | decision-specific | Not implemented |
| Pod and container health | Kubernetes exporters | Exporter-owned | exporter-specific | Platform-owned |
| GPU utilization, memory, and hardware errors | Accelerator exporter | Exporter-owned | exporter-specific | Platform-owned |

The Frontend upstream queue gauge is not a fleet-wide queue total. A request
can contribute to multiple scaling targets. In E/P/D workflows its lifetime can
span encoder or prefill work and decode admission; in aggregate workflows it is
usually shorter. Queries must not blindly sum it across targets.

## Stage latency sample eligibility

Current model-server JSON histograms do not observe every request:

- TTFT is recorded only when a first non-empty token is produced.
- E2E and TPOT are recorded only for successful `Stop`, `Length`, or
  `Repetition` terminal outputs.
- TPOT requires more than one generated token.
- Error, abort, consumer-close, and unsuccessful requests do not contribute E2E
  or TPOT samples.
- Once the bounded response channel applies backpressure, later latency samples
  for that request are omitted to avoid measuring response-consumer delay.

Histogram count is therefore an eligible-sample count, not total request count.
The Foretoken bucket arrays mirror the pinned vLLM source but are hard-coded in
`model-server/src/backend_telemetry.rs`. Native vLLM histograms retain their own
upstream semantics and are not assumed identical to these JSON histograms.

## Model-server producer MVP

The first code PR adds `GET /metrics` to the existing model-server internal HTTP
router. The handler returns the complete output of
`vllm_metrics::METRICS.render()` so the pinned vLLM metrics are available for
scraping.

This is the initial integration surface, not a promise that every upstream
family is a permanently frozen Foretoken API. An allowlist or upgrade-diff
policy can be added later if it proves useful. Foretoken-owned admission and
stage histograms are not newly registered in this MVP.

The existing `/v1/internal/telemetry` JSON contract and autoscaling behavior
remain unchanged.

## Labels and lifecycle

Application-owned labels must be bounded. Request ID, session ID, user ID,
tenant ID, raw URL, prompt, generated text, error message, and trace ID must not
be metric labels.

Counters and cumulative histograms reset when their process or Pod restarts.
Queries must use reset-aware functions such as `rate()` or `increase()`. A
missing optional engine sample means unavailable, not zero.

A removed model group stops producing new samples and disappears from active
targets. Historical samples remain until Prometheus retention expires.
Dashboard queries must handle multiple replicas, rollouts, and stale targets.

## Platform integration

The MVP exposes `/metrics` on the existing model-server internal listener. It
does not add a separate port or broaden the Service's external exposure.

Foretoken integrates with the platform's existing Prometheus Operator, Grafana,
and Alertmanager. The repository does not deploy another monitoring stack. A
later PR can add ServiceMonitor or PodMonitor resources and adjust NetworkPolicy
or the metrics listener if deployment experience requires it.

## First code PR acceptance criteria

- `/metrics` is available whenever the existing model-server HTTP server is
  running, independent of admission open or closed state.
- The response uses
  `application/openmetrics-text; version=1.0.0; charset=utf-8`.
- The response terminates with `# EOF` followed by a newline.
- Tests verify that a known native registry metric is returned.
- The JSON telemetry API and autoscaling behavior remain unchanged.
- Scrape resources, dashboards, alerts, exporters, and profiling are excluded.

## Non-blocking follow-up decisions

- Select stable Kubernetes labels for ServiceMonitor or PodMonitor queries.
- Decide whether operational experience warrants a native-family allowlist.
- Decide which Foretoken-owned admission and stage metrics should be registered.
- Define Frontend TTFT, stream E2E, and terminal outcomes before implementing
  Frontend SLO metrics.

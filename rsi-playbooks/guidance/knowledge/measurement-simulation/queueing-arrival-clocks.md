<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Diagnose hidden queueing through arrival-relative latency measurement

English | [简体中文](queueing-arrival-clocks_zh.md) · [Knowledge](../README.md)

## Observations and Motivation

A service can appear to have stable latency while a concurrency cap or overloaded generator accumulates an upstream queue. Measuring only actual-send-to-response time omits requests' intended arrival times. Mean prompt/output lengths also conceal bursts, length correlations, and long requests that retain KV or interrupt decode.

## Principles and Analysis

For scheduled arrival tₐ, actual send tₛ and completion t𝒻:

```text
arrival_latency = t𝒻−tₐ = send_delay + request_latency
send_delay = tₛ−tₐ
```

Closed-loop clients reduce offered load when service slows: C clients with response time R and think time Z produce roughly C/(R+Z). Externally scheduled arrivals do not slow with completion. Dropping their planned times causes coordinated omission.

An explanatory M/M/1 model has mean response time `1/(μ−λ)` under Poisson arrivals, independent exponential service, and a stable single server. At λ=90 requests/s, μ=100 gives 100 ms and μ=95 gives 200 ms. Continuous batching violates these assumptions; the example explains queue sensitivity, not engine performance.

## Design

Preserve each request's scheduled offset, actual send, first response, terminal time, outcome, and input/output length pair. Keep arrival order and correlations intact. Separate admission duration from drain duration and retain unfinished attempts.

A timestamp/length trace such as Azure's 2024 dataset contains no prompt text, so synthetic matching lengths do not recover real prefix reuse or stopping behavior. Label dispatch as uncapped external scheduling, capped replay, or closed-loop according to its actual behavior, not the presence of a trace file.

## Implementation

Compute from one monotonic clock where possible:

```text
for each planned request i:
    scheduled = run_origin + trace_offset[i]
    retain scheduled while waiting for client admission
    record send, first_event and terminal when they occur
    send_delay[i] = send−scheduled
    request_latency[i] = terminal−send
    arrival_latency[i] = terminal−scheduled
    arrival_ttft[i] = first_event−scheduled
count planned, sent, completed, failed and still-pending requests
aggregate timing distributions only for their declared population
```

Rising send delay with flat request latency exposes upstream queueing; both rising indicate delay on both sides of send. For a stable matching population, `L=λW` checks mean in-flight work against rate and residence time. Do not mix sent concurrency with arrival-relative W.

Foretoken preserves planned offsets and reports send/replay delay and trace-relative timing; its trace record uses `open_loop=False`, while ordinary latency summaries retain the request clock. AIPerf similarly distinguishes active from credit-relative effective latency.

## Impact and Results

Arrival-relative accounting reveals waiting hidden by client admission and separates generator delay from service delay. Preserving trace correlations distinguishes burst and long-request effects from average-length effects. Keeping failures and unfinished work prevents completion-only selection from making overload look healthy. These calculations diagnose missing demand; they do not increase the generator's sustainable issue rate.

## References

- [Azure inference dataset description](https://github.com/Azure/AzurePublicDataset/blob/master/AzureLLMInferenceDataset2024.md): 2024 code/conversation trace schema, privacy omission, and collection dates.
- [AIPerf effective versus active metrics](https://github.com/ai-dynamo/aiperf/blob/e1daadd10faa395100384358131d784d6858c5ae/docs/reference/effective-vs-active-metrics.md): credit queueing, effective latency, and success-only versus adjusted metrics.
- [Foretoken scheduling](../../../../benchmarks/src/benchmarks/runs/scheduling.py), [HTTP run ownership](../../../../benchmarks/src/benchmarks/runs/http.py), and [metrics](../../../../benchmarks/docs/metrics.md): at `e6ae3df6`; planned arrivals, admission limits, draining, and trace-relative timing.

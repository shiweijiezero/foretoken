<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Diagnose SLO failures through request-level compliance attribution

English | [简体中文](slo-request-attribution_zh.md) · [Knowledge](../README.md)

## Observations and Motivation

Higher throughput can accompany fewer requests meeting latency requirements. Successful-only percentiles also hide failures and cancellations, while token totals favor long outputs. Diagnosis needs to identify which attempted requests met all service-level objectives (SLOs), and which condition each failed request violated.

## Principles and Analysis

For N attempted requests tracked through terminal outcomes over T, let gᵢ=1 only for success with every required condition met. Attainment is `Σgᵢ/N`, request goodput `Σgᵢ/T`, and token goodput `Σgᵢnᵢ/T`. Token goodput needs actual output counts for compliant requests.

As an explanatory example, 100 completions/s with 60 compliant versus 80/s with 76 compliant ranks throughput and request goodput differently. If two criteria each have 95% attainment, their joint attainment can be as low as 90%; multiplying assumes independence.

Likewise, `p99(Q+P+D+O)` is not the sum of stage p99s. Different requests dominate each stage, and stages can overlap. One long pause can violate maximum-gap requirements despite acceptable mean TPOT.

## Design

Score joint compliance per attempt before aggregation. Retain a violation set for each request and its workload class, rather than assigning every failure to one mutually exclusive cause. Select end-to-end tail requests first, then inspect their causal stages and overload/rank/cache context.

Use a declared cohort and time denominator. Whole-run T includes drain and excludes warmup; fixed steady-state windows count their completions and separately retain unfinished work. Request samples within a burst are correlated, so uncertainty resampling follows independent runs or time blocks.

## Implementation

A request-level calculation is:

```text
for attempt in measured_attempts:
    violations = {} if success else {failure}
    for required criterion (metric, threshold):
        value = request_metric(metric)
        if value is missing or value > threshold:
            violations.add(metric)
    compliant = (violations is empty)
    retain request ID, class, compliant and violations
attainment = compliant_count / attempted_count
request_goodput = compliant_count / T
token_goodput = sum(compliant output counts) / T
    only if all needed counts are available
for each class and violation set: aggregate counts
select requests by total-latency tail; inspect their own stage records
```

Foretoken's TTFT uses the first non-empty `choices`, potentially role/finish; its request ITL SLO uses the maximum received-chunk interval. Usage-only chunks do not advance timing. AIPerf's average decode interval is a different criterion. Foretoken one-second curves assign all tokens to request completion windows, not device-production instants; combine windows using counts, not an unweighted mean of attainment fractions.

## Impact and Results

Joint scoring detects requests that pass one condition and fail another, and retains overlap between violation types. Per-class counts expose apparent tail improvements caused by changed completion mixtures or discarded slow requests. Tail-request attribution avoids adding unrelated stage percentiles. Consistent timing and token sources prevent chunk batching or missing usage from appearing as an inference improvement.

## References

- [Foretoken metric definitions](../../../../benchmarks/docs/metrics.md), [chunk timing](../../../../benchmarks/src/benchmarks/integrations/streaming.py), and [HTTP runner](../../../../benchmarks/src/benchmarks/runs/http.py): at `e6ae3df6`; joint criteria, failures, received chunks, and completion-window curves.
- [AIPerf metrics reference](https://github.com/ai-dynamo/aiperf/blob/e1daadd10faa395100384358131d784d6858c5ae/docs/metrics-reference.md) and [effective metrics](https://github.com/ai-dynamo/aiperf/blob/e1daadd10faa395100384358131d784d6858c5ae/docs/reference/effective-vs-active-metrics.md): metric populations and normalization differ.
- [Vidur, MLSys 2024](https://www.microsoft.com/en-us/research/wp-content/uploads/2024/05/vidur_mlsys24.pdf), §§7.2–7.3: near-capacity error amplification and workload-dependent SLO/cost trade-offs in the evaluated systems.

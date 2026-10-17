<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Improve Goodput Through Predicted Deadline-Feasibility Admission

English | [简体中文](deadline-aware-admission_zh.md)

## Observations and Motivation

During overload, forwarding every arrival can consume GPU time on work already unable to meet its objective, making feasible requests late too. Selecting the least-bad worker does not establish feasibility. Admission decides whether to run, wait or reject; routing chooses a destination for eligible work.

## Principles and Analysis

A 300-ms TTFT objective leaves 200 ms after 100 ms of preparation and ingress waiting. If the best worker needs 120 ms of queueing and 100 ms of prefill, it is already 20 ms short. Resetting a downstream timeout to 300 ms hides the elapsed ingress time.

`remaining_slack = arrival_deadline - now - estimated_downstream_latency`.

TTFT feasibility includes first-token work; a completion deadline also includes generation. Request deadline, queue TTL and TPOT are distinct contracts. Negative slack indicates predicted infeasibility, with false rejection or overload depending on model error.

SCORPIO coordinates TTFT rejection, deadline-ordered queues and TPOT feasibility. Its virtual batch size sums heterogeneous token-rate proportions, and predicted TPOT must meet the strictest target in the proposed running set. Admission and decode credits therefore depend on the same calibrated latency and service-rate assumptions.

## Design

Keep the original arrival time and trusted objectives through every stage. Ingress owns queue state, admission reservations and response outcomes; the engine owns batch feasibility and physical resource release. Cluster orchestration supplies slower capacity response. A scorer ranks workers without mutating global admission capacity.

llm-d's saturation/flow-control admission and predicted-headroom scorer are separate paths; unfavorable scores can still choose idle or least-deficit endpoints. Production Stack forwards priority rather than implementing predictive rejection. Foretoken's role-based waiting, concurrency accounting and deadline outcomes provide the queue/lifecycle boundary.

## Implementation

1. Capture arrival time, resolve trusted objectives and derive their absolute deadlines without restarting clocks downstream.
2. Estimate each eligible destination's remaining first-token work: queueing, cache readiness, uncached prefill and delivery overhead.
3. Check first-token slack and the resulting running set's TPOT/memory feasibility using deployment-specific model, dtype, backend and parallelism estimates.
4. Dispatch feasible work with a capacity reservation. Otherwise wait only while the queue policy and remaining deadline permit a useful future dispatch; reject or expire infeasible work with a recorded outcome.
5. Reassess waiting work when capacity changes. On cancellation or completion, release ingress accounting; the engine releases its own resources asynchronously.

## Impact and Results

Feasibility-aware admission avoids spending capacity on predicted-late work and can preserve more qualified completions. Aggressive rejection can disadvantage long prompts, strict classes or poorly modeled tenants; waiting helps transient bursts but consumes deadline slack.

`goodput = completed requests meeting all declared SLOs / elapsed time`.

Arrival-based attainment counts rejects and timeouts as unmet unless a different benchmark contract is explicit. Of 1,000 arrivals, completing 650 qualified requests after rejecting 300 gives 65% arrival attainment, not the 93% admitted-only rate. Compared with 500 qualified completions, this improves useful service without changing the denominator. SCORPIO likewise counts rejected requests as SLO-unmet.

## References

- [SCORPIO, 2505.23022v2, 2026-08-26](https://arxiv.org/html/2505.23022v2): coordinated TTFT/TPOT admission and rejected-request accounting.
- [llm-d admission code, commit 5253f555](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/requestcontrol/admission.go).
- [llm-d latency scorer, same revision](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/scheduling/scorer/latency/plugin.go): ranking is not rejection.
- [Production Stack routing, commit 4e076e8d](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py): implemented priority forwarding.
- [Foretoken admission](../../../../data-plane/frontend/src/admission/src/lib.rs) and [arrival capture](../../../../data-plane/frontend/src/server/src/admission.rs): current outcome and timing boundaries.

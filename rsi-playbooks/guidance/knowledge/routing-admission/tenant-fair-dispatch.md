<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Avoid Tenant Starvation: Fair Queues and Bounded Dispatch

English | [简体中文](tenant-fair-dispatch_zh.md)

## Observations and Motivation

A global FIFO queue can place one tenant's request behind another tenant's thousand-request burst. A strict priority order can also starve lower-priority traffic. Fair dispatch separates eligible flows and shares finite capacity without leaving abandoned requests permanently charged.

## Principles and Analysis

Priority chooses which class proceeds first; fairness shares service among eligible flows; capacity limits bound active work; expiry bounds useful waiting. None implies the others. llm-d's `globalstrict` chooses queue heads within a priority band but explicitly provides no tenant fairness.

Least-attained-service (LAS) favors less-served programs rather than equal request counts. llm-d measures weighted input/output service, with output weighted twice input, and decays history in wall-clock time. Its turn-priority alternative uses `score = turnNumber + timeWeight × headWaitSeconds`. This favors dependent session progress and locality, not equal tenant service. Waiting credit is bounded by `timeWeight × requestTTL`, so it is not inherently a starvation guarantee.

## Design

Use per-flow queues under trusted identities, then choose a queue head within the applicable priority/capacity constraints. Caller-controlled identities can split one tenant into many apparent flows. Turn depth requires one fairness identity per session; a shared default flow's aggregate count is not meaningful session depth.

The request layer owns queueing, reservations, expiry and dispatch. The engine owns actual token service, and orchestration owns replicas. Define accounting scope across frontends: independent local counters do not establish service-wide fairness. Foretoken already separates fair admission from instance scoring; a scorer does not own another queue.

## Implementation

1. Resolve trusted tenant/session identity and priority, then enqueue under the existing admission owner with an expiry time.
2. Remove expired or canceled heads and apply priority-band and saturation eligibility. llm-d priority holdback restricts lower-priority dispatch sooner as saturation rises.
3. For LAS, decay each flow's retained service by elapsed wall-clock time and compare attained service with head wait. For turn-priority, compare session depth plus weighted wait.
4. Reserve capacity, dequeue the selected head and dispatch it through instance routing; record in-flight state.
5. On completion, charge reported token usage and release in-flight accounting. Error/cancellation cleanup releases reservations; the engine handles physical resource cleanup.

AIBrix's basic VTC charges estimated routing usage, not actual completion reconciliation; Production Stack forwards priority. Engine-level VTC/DLPM bounds describe a different service scope from these gateway policies.

## Impact and Results

Per-flow queues let a dispatcher serve another tenant without first draining the burst. LAS accounts for unequal token cost; turn-priority can favor cache-local dependent turns while delaying newcomers. In llm-d's cited defaults, `0.05 × 60s = 3` turn-equivalents of waiting, so a turn-one newcomer cannot overtake a turn-five session before expiry. Abandoned in-flight state can also block eviction. Bounded waiting and lifecycle release limit retained work, while actual decode fairness remains the engine's responsibility.

## References

- [llm-d global-strict policy, commit 5253f555](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/flowcontrol/fairness/globalstrict/README.md).
- [Program-aware fairness, same revision](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/flowcontrol/fairness/program-aware/README.md).
- [Priority holdback, same revision](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/flowcontrol/usagelimits/priorityholdback/README.md).
- [AIBrix VTC basic, commit d0500b03](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/pkg/plugins/gateway/algorithms/vtc/vtc_basic.go) and [Production Stack routing, commit 4e076e8d](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py).
- [VTC methods](https://arxiv.org/html/2401.00588v2) and [DLPM/D2LPM, 2501.14312v1](https://arxiv.org/html/2501.14312v1).
- [Foretoken admission owner](../../../../data-plane/frontend/src/admission/src/lib.rs).
- [engine fairness](../engine-scheduling/token-fairness-counters.md)

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance Token Service: Weighted Counters and Locality-Aware Deficits

English | [简体中文](token-fairness-counters_zh.md)

## Observations and Motivation

Equal request counts do not mean equal inference service. Long generations consume decode capacity repeatedly after admission, so a fair ingress order alone cannot equalize their service with short requests. Token accounting distinguishes this cost while locality-aware selection preserves useful prefix reuse.

## Principles and Analysis

VTC measures `service = wp × input_tokens + wq × output_tokens`. Output weights may be larger to reflect decode cost; they are a policy approximation, not universal GPU milliseconds. Weights 1 and 2 give 1,200 units for a 1,000-input/100-output request and 2,100 for a 100-input/1,000-output request.

VTC admits from the queued client with the smallest counter. Its bounded-service-difference result assumes bounded inputs and batches, positive capacity and no preemption; it is not a request-latency guarantee.

DLPM combines longest-prefix matching with positive-deficit eligibility. Charging uncached input extension plus output measures executed work more closely than charging all logical input. Cache-hot clients then receive more logical tokens for the same service share. The chosen accounting therefore defines whether fairness concerns compute, delivered tokens or entitlement.

## Design

VTC orders admission by attained service and charges execution as it happens. Counter lifting prevents idle clients from banking extra credit. DLPM permits cache-local runs until a client's credit is exhausted; a larger quantum improves locality but increases short-term service disparity. D2LPM adds client-worker deficits and cache-aware candidate selection while retaining worker-local fairness.

The engine owns actual batch service; the request layer owns trusted identity, fair ingress and dispatch. Per-router counters establish only their declared local scope, not a global engine-service bound. Cluster orchestration owns capacity, not a second copy of fairness state.

## Implementation

For VTC:

1. When a client adds its first waiting request, lift its retained counter to at least the minimum among already-waiting clients. If that queue was empty, use the last client that left it. Running-only clients are not the comparison set.
2. When capacity is available, admit a request from the waiting client with the smallest counter.
3. Charge prompt service by `wp` and each generated token by `wq`; preserve charged service when the client becomes idle.

For DLPM, admit a longest-prefix candidate only if its client's deficit is positive. When all queued clients have nonpositive deficits, replenish active clients whose deficits are nonpositive with a quantum. Deduct uncached extension and generated-output service from their credit.

AIBrix `vtc-basic` instead charges estimated input/output usage at routing and chooses pods using tracked usage and load; the cited file does not reconcile actual completion tokens. llm-d's decayed LAS and turn-priority operate on dispatch queues. These are distinct accounting contracts.

## Impact and Results

Weighted counters prevent long-output traffic from appearing equal to much cheaper requests. Deficits permit locality without unlimited service domination, with bounds dependent on request cost and quantum. Token-fair admission can still leave unequal in-flight decode service; dispatch fairness and execution fairness differ. Foretoken already separates role-based waiting and fair dispatch from instance scoring, allowing each owner to retain its own service scope.

## References

- [VTC, arXiv 2401.00588v2](https://arxiv.org/html/2401.00588v2): counter lifting, weighted service, and fairness assumptions.
- [Locality-aware Fair Scheduling, 2501.14312v1, 2025-01-24](https://arxiv.org/html/2501.14312v1): DLPM/D2LPM and deficit/locality trade-offs.
- [AIBrix `vtc_basic.go`, commit d0500b03](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/pkg/plugins/gateway/algorithms/vtc/vtc_basic.go): actual gateway accounting and pod selection.
- [llm-d program-aware fairness, commit 5253f555](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/flowcontrol/fairness/program-aware/README.md): current dispatch strategies and state scope.
- [Foretoken routing](../../../../data-plane/frontend/src/router/README.md): admission and instance selection remain separate.

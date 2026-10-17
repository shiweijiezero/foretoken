<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Improve SLO Attainment: Batch Sizing and Decode Cadence

English | [简体中文](slo-decode-cadence_zh.md)

## Observations and Motivation

With heterogeneous latency objectives, decoding every active request every iteration can spend scarce capacity on streams that could wait. Always prioritizing decode can instead starve queued prefills of first-token opportunities. Batch selection needs both TTFT deadlines and the token service rate each running request requires.

## Principles and Analysis

SCORPIO defines token requirement proportion as `TRP = min_target / request_target`, taking the minimum TPOT target over the current running set. Targets of 50 ms and 100 ms give proportions 1 and 0.5. Adding that credit each decode iteration and spending one credit per selection serves the relaxed stream roughly every other iteration. Recompute proportions when the strictest running target changes.

The virtual batch size is the sum of proportions; sequence lengths and this size feed a TPOT estimate. A 90-ms iteration cannot meet a 50-ms target through credit arithmetic. Mean TPOT also does not bound individual token gaps.

SOLA uses normalized TTFT/TPOT pressure to improve one latency while constraining the other. Andes instead compares delivered-token progress with an application consumption timeline; buffered streams can wait while empty-buffer streams catch up. That objective applies only when consumption semantics permit deferred delivery.

## Design

Coordinate deadline-ordered prefill with rate-differentiated decode and memory feasibility. Admission uses the same latency model and virtual-batch assumptions as execution. The engine owns membership, cadence, prefill progress and cancellation; the request layer owns original arrival time, trusted objectives, ingress waiting and instance selection. A routing priority integer alone cannot enforce next-token timing.

## Implementation

1. Obtain per-request TTFT/TPOT objectives and calibrate execution costs for the model, dtype, backend, topology and parallelism.
2. For the proposed running set, compute its strictest TPOT target, each TRP and the virtual batch size; check predicted latency and memory feasibility.
3. At each decode iteration, add TRP to each request's credit, select requests with credit at least one, then deduct one from selected requests.
4. Order waiting prefills by first-token deadline pressure and fit feasible prefill work around decode obligations.
5. Update progress and cost estimates from executed batches. SOLA adjusts phase ordering and workload from measured SLO pressure instead of using the TRP schedule.

vLLM's cited `next_decode_eligible_step` matches sampled-token broadcast cadence in its V2 asynchronous pipeline-parallel path; it is not a per-request SLO policy.

## Impact and Results

Differentiated cadence reduces contention from relaxed streams and leaves capacity for stricter generation or waiting prefill. Skipping iterations increases gap variance and does not release resident KV. Length errors distort memory and latency estimates, while aggregate optimization can sacrifice a small class without explicit class objectives. Goodput counts completed requests meeting both declared objectives per second; arrival-based attainment includes rejects, timeouts and cancellations rather than hiding them in an admitted-only denominator.

## References

- [SCORPIO, arXiv 2505.23022v2, 2026-08-26](https://arxiv.org/html/2505.23022v2): token requirement proportions, virtual batch size, and admission/queue/batch coordination.
- [SOLA, MLSys 2025, §§4–5](https://proceedings.mlsys.org/paper_files/paper/2025/file/bc82dbfbfa43232be85b8d9838f49c3e-Paper-Conference.pdf): constrained workload control and state-aware ordering.
- [Andes, arXiv 2404.16283v1](https://arxiv.org/html/2404.16283v1): delivery-curve QoE and preemption-aware selection.
- [vLLM scheduler, commit 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/sched/scheduler.py): current eligibility, budget, and priority mechanisms.

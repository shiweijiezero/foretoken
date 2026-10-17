<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Avoid KV Thrashing Through Memory-Aware Batch Selection

English | [简体中文](kv-pressure-admission_zh.md)

## Observations and Motivation

A request can fit at admission and exhaust KV capacity as its output grows. Repeated eviction and recomputation then spend GPU work rebuilding progress rather than producing useful tokens. Memory-aware selection keeps the running set feasible without reserving every request's maximum possible output.

## Principles and Analysis

Two requests with 8K inputs and predicted 1K outputs may initially fit; if both generate 8K outputs, their combined footprint nearly doubles. For ordinary dense attention:

`KV_bytes ≈ resident_tokens × 2 × layers × KV_heads × head_dim × element_bytes`.

Actual per-rank allocation differs with TP, MLA, hybrid-state groups, shared prefixes and block rounding. Cached input and future output also have different sharing opportunities.

Current-footprint admission risks future recovery; maximum-output reservation wastes capacity when most generations end early. SOLA predicts future peak memory before admitting work and excludes swapping/preemption from its simplified algorithm. SCORPIO combines length prediction with latency-aware admission. Underprediction can make either future running set infeasible.

## Design

Choose a running set using current allocation and estimated remaining growth. Defer new requests when they would overfill the set. If growth still exhausts memory, recover through the engine's existing preemption lifecycle: freeing a large old request saves memory but loses expensive progress, while evicting short requests may harm more deadlines.

Only the engine knows which blocks are reclaimable and can resume requests consistently. The request layer can limit new dispatches to pressured instances; completion or cancellation releases existing KV through the engine. Cluster orchestration adds capacity on a slower timescale.

## Implementation

1. Estimate the added per-rank blocks for each candidate, accounting for reusable prefixes and predicted output growth.
2. Admit work only if the selected set fits the memory envelope and execution budgets.
3. In vLLM V1, call `allocate_slots`; if it fails, select a running victim whose blocks can actually be freed. Priority scheduling chooses the greatest `(priority, arrival_time)` pair; other scheduling selects the running-list tail.
4. Preemption frees KV/encoder state, resets computed progress, clears draft tokens and returns the request to waiting. Restore any budget already assigned to that victim in the current schedule.
5. Resume by rebuilding missing state from retained history, reusing available prefix blocks. In-flight execution and deferred frees determine when memory is physically reusable.

## Impact and Results

Feasible batch selection reduces KV thrashing and the recovery work that displaces useful generation. vLLM V1 defaults to recomputation rather than CPU swapping. More conservative admission reduces preemption but may leave memory idle; optimistic length estimates increase concurrency until long outputs reveal the error. Prefix reuse can salvage progress, so resetting computed-token state does not imply recomputing every historical token.

## References

- [vLLM scheduler, commit 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/sched/scheduler.py): `allocate_slots`, `_preempt_request`, victim selection, and deferred-free constraints.
- [vLLM optimization guide](https://docs.vllm.ai/en/latest/configuration/optimization/): preemption signals and current operational controls.
- [SOLA, MLSys 2025, §5.1](https://proceedings.mlsys.org/paper_files/paper/2025/file/bc82dbfbfa43232be85b8d9838f49c3e-Paper-Conference.pdf): future peak-memory checks at batch admission.
- [SCORPIO, arXiv 2505.23022v2, 2026-08-26](https://arxiv.org/html/2505.23022v2): prediction-dependent admission under heterogeneous objectives.

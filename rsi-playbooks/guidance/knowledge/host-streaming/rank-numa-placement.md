<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce rank stalls through NUMA-aware host placement

English | [简体中文](rank-numa-placement_zh.md) · [Knowledge](../README.md)

## Observations and Motivation

A multi-rank replica adds scheduler processes, device workers, communication progress, tokenizer threads, and output processing. More parallelism can reduce device work per rank while increasing runnable host tasks. Low machine-wide CPU utilization can coexist with starvation of the core or cgroup quota feeding the next collective. vLLM's physical-core guidance budgets API, engine-core, and GPU-worker processes separately.

## Principles and Analysis

Quota, affinity, and memory locality constrain different resources. A cgroup granted 200 ms per 100 ms period has roughly two cores of aggregate quota even if its affinity allows eight CPUs. Affinity does not reserve cores or raise quota. NUMA policy normally places pages on allocation or first touch; binding a worker after allocation does not relocate its buffers.

For collective arrival times aᵣ, completion cannot precede max(aᵣ) plus remaining transfer work. As an explanatory example, three ranks arrive at 1 ms, one at 5 ms, and transfer takes 1 ms. Early ranks show roughly 5 ms inside the collective, while the last shows 1 ms. The dominant 4 ms skew is not link transfer.

## Design

Place each device worker's CPU execution and repeatedly accessed host buffers near its GPU. Reserve room for API, scheduler, output, and communication tasks rather than assigning every physical core to worker threads. Allocate pinned buffers after establishing affinity and allowed memory nodes. Shared bandwidth-heavy buffers may benefit from interleaving instead of strict binding.

Keep a per-rank view of quota throttling, runnable waiting, allocation locality, and collective arrival. Align distributed traces by step/collective identity and synchronized clocks, not unrelated node timestamps.

## Implementation

On a Linux NUMA host:

1. Map each GPU's PCI locality to NUMA nodes and physical cores; intersect this with the container's effective CPU and memory sets.
2. Read the cgroup quota hierarchy, including parent limits. Budget simultaneous physical-core demand across API, engine, workers, and library thread pools.
3. Set worker affinity and memory policy before creating and touching metadata or pinned-copy buffers. Initialize pages on the consuming node; recreate or explicitly migrate existing remote allocations when changing placement.
4. For each collective, group records by communicator, operation and step. Compute arrival skew `max(aᵣ)−min(aᵣ)` and associate the late rank with preceding CPU runnable wait, throttling, host copies, and device work.
5. Distinguish quota stalls from remote-memory access using those records; aggregate CPU percentage alone cannot identify either.

SGLang's overlap path updates CPU length mirrors only for consumers that need them. Foretoken's CPU curves are counter-derived observations, not per-rank scheduling traces.

## Impact and Results

Local allocation reduces remote-socket traffic for worker-owned buffers; physical-core placement reduces competing runnable tasks and sibling-thread contention. Sufficient quota prevents periodic budget exhaustion. These changes can reduce collective arrival skew without changing network bandwidth. Dedicated cores reduce replica packing capacity, and strict binding can limit shared-memory bandwidth. Hyperthreads are not equivalent to independent physical cores.

## References

- [vLLM optimization documentation](https://docs.vllm.ai/en/latest/configuration/optimization/): physical-core guidance and busy-loop scheduler contention.
- [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) and [NUMA memory policy](https://docs.kernel.org/admin-guide/mm/numa_memory_policy.html): quota, effective CPU sets, pressure, allocation policy, and migration semantics; live kernel documentation.
- [SGLang overlap utilities](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/managers/overlap_utils.py): device/CPU metadata boundaries.
- [Nsight Systems guide](https://docs.nvidia.com/nsight-systems/UserGuide/index.html): process scope, CPU/device correlation, timestamp normalization, and distributed clock alignment.
- [Foretoken CPU observations](../../../../benchmarks/docs/metrics.md#cpu-and-memory-observations): counter-derived estimates and trailing-window curves; at `e6ae3df6`, not per-rank scheduling traces.

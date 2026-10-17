<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance expert hotspots with windowed replication and migration

English | [简体中文](expert-window-rebalancing_zh.md)

## Observations and Motivation

An expert-parallel layer completes when its slowest rank finishes. Equal stored expert counts do not ensure equal execution: language, task, layer, and request mix produce persistent routing hotspots. Windowed EPLB replicates and relocates expert weights to distribute that work while preserving the model's logical expert selection.

## Principles and Analysis

For estimated logical load `w_e` and replica count `r_e`, the default replication policy uses per-copy load `w_e/r_e`. With loads `[100,20,20,20]` and one extra slot, replicating the first expert lowers its estimated per-copy load to 50. That is a planning estimate, not a halving of GEMM latency.

The useful timescale is longer than a noisy batch but shorter than a sustained workload change. Short windows react sooner and chase noise; long windows lag a new request mix. Rebalance intervals count engine steps, whose wall-clock duration varies with load. Equal token loads also need not yield equal runtime when tiles and communication differ.

## Design

Collect global load over a history window, then choose a physical-to-logical mapping under the redundancy budget. When model groups permit hierarchical placement, the default policy packs expert groups across nodes, assigns replicas to high per-copy loads, and packs physical experts across GPUs. Preserve existing local slots where possible to avoid needless copies.

An asynchronous worker computes placement and stages required weights one layer at a time. Forward execution consumes the pending result and installs weights with their mapping before staging storage is reused. Quantization scales belong to the transferred expert. Captured execution must retain valid storage and mappings throughout the change.

## Implementation

1. Accumulate expert statistics from forward execution; at the configured interval, provide global estimates and the previous mapping to the default policy.
2. Allocate redundant copies by estimated per-copy load, pack slots across ranks, and retain unchanged local slots.
3. Build weight transfers. The optional migration scheduler coalesces each directed rank pair and batches disjoint endpoints, so a rank communicates with at most one peer per batch; local copies need no remote transfer.
4. Stage a layer's weights and scales, wait for writes to finish, and publish a pending result.
5. Let the forward-side consumer install the corresponding representation and mapping, acknowledge consumption, and release staging space for reuse.

The pinned vLLM default policy, async worker, and `migration_scheduler.py` own these stages. [Batch replica assignment](batch-replica-assignment.md) addresses fluctuations inside an existing placement instead.

## Impact and Results

Replication reduces persistent hotspot concentration without changing the logical router. Preserving slots saves migration bytes; endpoint-disjoint scheduling avoids competing transfers in the same batch. The `[100,20,20,20]` example explains allocation rather than reporting measured speedup.

Redundant weights and scales consume memory otherwise available to KV. Asynchronous transfer still uses network and device bandwidth, so migration can increase inference latency even without a blocking host call. Rapidly rotating hotspots can disappear before the window reacts. The net effect depends on sustained skew, observation lag, transfer interference, and lost cache capacity—not merely a more balanced histogram.

## References

vLLM commit [6849ca69](https://github.com/vllm-project/vllm/tree/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9), specifically [default policy](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/eplb/policy/default.py), [async worker](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/eplb/async_worker.py), and `migration_scheduler.py`; also the [official EP deployment guide](https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/).

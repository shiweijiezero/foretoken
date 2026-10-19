<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Preserve active generations through draining and state handoff

English | [简体中文](generation-drain-handoff_zh.md)

## Observations and Motivation

Removing a decode worker can interrupt minutes of generation and discard warm KV. Reducing replicas slowly does not stop new dispatches or preserve accepted work. Safe retirement separates admission closure, route withdrawal, request completion and state release; persistent prefix storage is not a resumable request checkpoint.

## Principles and Analysis

Closing admission before route withdrawal prevents stale routers from adding work. Retirement then waits for observers to install the withdrawal and accepted requests to finish. A deadline bounds retained capacity, but expiry means incomplete drain rather than zero-loss completion.

Natural drain preserves execution while holding the source GPU for the remaining generation lifetime. Live migration can shorten that retention only with compatible destination headroom and an acceptable pause. Copying 4 GiB over an effective 25 GB/s link has a 172-ms payload lower bound, excluding synchronization and state written during copying. This arithmetic is not a migration measurement.

Compatibility includes checkpoint, KV dtype, TP/DP layout and runtime state. GQA, MLA and recurrent caches differ; compatible weights alone do not establish cache-layout compatibility. Migration traffic can compete with inference on surviving workers.

## Design

Use admission closure and acknowledged withdrawal as the retirement boundary. Accepted requests remain source-owned until completion or a supported, acknowledged handoff. Shared prefix storage follows its own lifecycle; it cannot substitute for the active request's position, sampling state and cleanup ownership.

The controller owns worker retirement, frontends own route observation and request cleanup, and the engine owns continuation state. Foretoken's Group drain closes model-server admission, checks every surviving frontend's snapshot generation, observes running requests and releases its finalizer on completion or recorded timeout. Its P/D and E/P/D workflows retain cross-stage cleanup; this is retirement, not live migration.

## Implementation

For natural drain:

1. Close source admission and publish route withdrawal.
2. Wait for surviving frontend snapshots to acknowledge the withdrawal generation.
3. Observe accepted running requests until quiescence or the drain deadline.
4. Record completion or timeout distinctly, then release the retirement finalizer and resources.

For engine-supported migration, reserve a compatible destination, copy existing KV and catch up new writes. Pause at a consistent execution boundary; transfer token position, sampling RNG, speculative/hybrid state and request lifecycle. Hand off ownership, obtain destination acknowledgement, then release the source. Reissuing the prompt is replay, not transparent continuation.

Dynamo's shutdown utility unregisters endpoints and invokes optional timed callbacks; drain semantics depend on those callbacks. AIBrix's cited helper annotates pods and deletes on timeout without verifying engine quiescence. Production Stack endpoint refresh does not itself migrate active state.

## Impact and Results

Acknowledged drain prevents new work from racing retirement and preserves accepted generations when they complete within the deadline. It costs retained GPU time; local-cache loss also affects later requests. Migration trades that time for transferred bytes, destination memory and interruption overhead. TRE's A100 40GB hot-switch prototype reports second-scale sleep/wake for 7B/8B/14B models, recovering failed drains by reissuing work rather than migrating KV; those switching results do not demonstrate uninterrupted generation handoff.

## References

- [Foretoken Group drain owner](../../../../control-plane/controllers/model/drain.go)
- [Foretoken request workflow](../../../../data-plane/frontend/src/server/src/runtime/workflow.rs)
- [Dynamo graceful-shutdown utility](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/common/utils/graceful_shutdown.py)
- [AIBrix drain controller helper](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/pkg/controller/drain/drain.go)
- [Production Stack router code](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py)
- [TRE paper](https://arxiv.org/html/2609.29160v1)
- [KV-cache knowledge](../kv-cache/)

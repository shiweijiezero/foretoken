<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce Recovery Latency: Sleep/Wake and State Reuse

English | [简体中文](recovery-state-reuse_zh.md)

## Observations and Motivation

Recovery time depends on what survives: checkpoint files, host weight copies, a live memory owner, or request token history. “Warm” is not one state. A sleeping engine and a restarted process can have entirely different restoration paths even on the same GPU.

## Principles and Analysis

An illustrative level-1 wake moves W weight bytes at effective H2D bandwidth B, giving a payload floor `W/B`. It additionally pays allocation/restoration and any request replay. Level 2 adds source loading and transformations. For example, keeping 40 GiB of host weights avoids some reload work but consumes that RAM; memory pressure can offset restoration savings.

## Design

Choose a recovery path from surviving state. Level 1 relies on host weight backups; level 2 needs a weight source; IPC reuse relies on a live daemon and matching placement; an allocation artifact requires tensor reconstruction metadata. Request recovery separately restores compatible KV or replays the exact token history.

Publish an engine for scheduling only after weights, required buffers and KV allocations are ready. A new weight revision invalidates the assumptions behind old KV. Memory ownership and physical GPU identity remain part of recovery, rather than being inferred from a successful process restart.

## Implementation

The sleep/wake sequence is:

1. Level 1 backs weights into CPU memory and discards KV; wake restores weights from those copies and rebuilds the required allocations.
2. Level 2 discards weights/KV but retains required buffers on CPU. Wake weights, explicitly reload them, then wake KV; waking allocations alone does not recover discarded data.
3. Restore request state by loading compatible external KV or replaying the input and already-generated token IDs before continuing sampling.

The IPC loader maps daemon-owned CUDA tensors in zero-copy mode or clones them into engine-owned storage. Model, parallelism, role and GPU identity must match. Zero-copy requires the daemon to survive and is incompatible with sleep-mode weight offloading. Dynamo GMS restores allocation bytes into a fresh server; the consumer reconstructs tensor views with compatible allocation granularity, and client/sidecar must resolve to the same physical GPU.

ServerlessLLM request migration first replays a token snapshot on the target, then sends the additional tokens generated before handover for a second replay. This preserves produced tokens instead of resampling them.

## Impact and Results

Level-1 wake avoids storage reads and repeated weight interpretation but pays H2D restoration and retains host-weight memory. Level 2 releases more memory and adds explicit loading. Daemon-backed zero-copy avoids a new weight copy but couples availability to that daemon; clone mode spends device memory and copy time to give the engine ownership.

Token replay avoids moving a full KV history and instead spends prefill computation. External KV reuse can avoid replay when cache semantics and integration match, but weight restoration alone does not make every in-flight request resumable.

## References

- [current sleep guide](https://docs.vllm.ai/en/latest/features/sleep_mode/)
- [weight-cache IPC loader](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/weight_cache/ipc_loader.py)
- [GMS artifact implementation](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/gpu_memory_service/v1/snapshot/weight_artifact.py)
- [ServerlessLLM's original method](https://arxiv.org/html/2401.14351v1)

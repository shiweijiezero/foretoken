<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce device idle gaps through host/device execution overlap

English | [简体中文](host-device-overlap_zh.md) · [Knowledge](../README.md)

## Observations and Motivation

A short decode step can leave the device waiting while the host selects requests, constructs metadata, submits kernels, and consumes results. Faster kernels expose this work. An `async` HTTP handler can also block other connections by running synchronous tokenization or parsing before its next suspension point. Device submission and HTTP responsiveness are separate bottlenecks.

## Principles and Analysis

Let host preparation take H and device execution take D. A serialized steady-state interval is H + D; independent next-step preparation can reduce it toward max(H, D), plus uncovered synchronization and result processing. As an explanatory example, H = 2 ms and D = 3 ms gives 5 ms serialized and 3 ms ideally overlapped. This is a pipeline interval, not full request latency.

Three constraints differ: Python bytecode threads ordinarily contend for the GIL; an asyncio loop does not preempt synchronous callbacks even if a native extension releases the GIL; and the next device input may depend on the current sampled token. A thread pool changes host execution, not that data dependency.

## Design

Keep scheduling and in-flight request state with the engine scheduler. Prepare independent metadata while the previous step runs; relay dependent token values on device where supported, then reconcile actual outputs with reserved placeholders. Preemption and cancellation must invalidate outstanding request state before late results are consumed.

Separately, move blocking rendering or tokenization into the engine's existing executor paths. Executor queue wait and CPU service time remain distinct, so thread concurrency does not become an implicit second admission policy.

## Implementation

The core pipeline is:

```text
reserve request slots and output placeholders for step s
submit s; publish its device-state handles and completion event
while s runs:
    prepare independent metadata for s+1
before s+1 consumes dependent state:
    wait on the producer event on the consuming stream
    resolve token/state references without an unnecessary CPU copy
on output delivery:
    reconcile placeholders for still-active request generations
    discard stale deliveries after preemption or cancellation
release buffers only after their last device consumer completes
```

vLLM's `AsyncScheduler` owns placeholder accounting and output reconciliation; SGLang's overlap utilities implement request-indexed publication and consumer fences. CPU sequence-length mirrors are still needed by some backends. vLLM's online renderer supplies executor wrappers for non-streaming, plain streaming, and parsed streaming work. Free-threaded CPython does not remove native locks or these dependencies; extension compatibility determines its actual GIL state.

## Impact and Results

Pipelining hides independent host work behind device execution and reduces exposed launch gaps. Executor isolation keeps one blocking callback from delaying unrelated HTTP streams, without reducing total CPU demand. In-flight buffers, reconciliation, executor queueing, and pipeline fill add costs. If device execution already dominates or every decision needs the latest CPU-visible output, little host time can be hidden.

## References

- [Python 3.14 asyncio development](https://docs.python.org/3.14/library/asyncio-dev.html) and [free-threading](https://docs.python.org/3.14/howto/free-threading-python.html): cooperative scheduling, executors, and extension compatibility.
- [vLLM AsyncScheduler](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/sched/async_scheduler.py) and [online renderer](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/renderers/online_derenderer.py).
- [PR #49396](https://github.com/vllm-project/vllm/pull/49396) and [Issue #57350](https://github.com/vllm-project/vllm/issues/57350): renderer executor integration.
- [SGLang overlap utilities](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/managers/overlap_utils.py): backend-specific synchronization differs.

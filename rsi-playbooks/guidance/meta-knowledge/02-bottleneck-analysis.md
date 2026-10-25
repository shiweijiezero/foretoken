<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 02 | Bottleneck Analysis

English | [简体中文](02-bottleneck-analysis_zh.md)

Use benchmarks, profiles, runtime metrics, and logs to understand actual performance. Combine source analysis with focused experiments to explain costs and find worthwhile optimization opportunities.

## Inspect runtime results

- Benchmarks, W&B, and CSV: compare performance across lengths, concurrency levels, and request distributions. Inspect per-request results, time series, and anomalous intervals.
- Runtime metrics: inspect GPU utilization, device memory, CPU, host memory, queues, execution batches, and cache hits.
- Logs: identify the effective startup configuration, compilation and graph capture, scheduling events, errors, and state changes.
- Profiles: inspect the duration, frequency, and ordering of CPU/GPU events, operators, communication, synchronization, and data copies.

Choose results relevant to the question, and check request lengths, actual batch sizes, cache state, and generation settings. See [evaluation metrics](../../../benchmarks/docs/metrics.md) for definitions, [observability](../../../observability/README.md) for observation tools, and the [experiment records guide](../../experiment-records.md) for artifact layout.

## Capture and inspect with Foretoken

Start with existing metrics, logs and Foretoken's profiling tools to see how the actual workload executes. Adding timers, per-step logging, forced synchronization or diagnostic requests can change execution order, batching and cache state, so the observed behavior may differ from an ordinary run. Add markers or other instrumentation only when existing traces lack information needed to answer the question. Make that code removable or switchable, and explain what it records and how it may affect execution.

- [PyTorch Profiler](https://docs.pytorch.org/tutorials/beginner/profiler.html): capture CPU/GPU events during model execution and inspect operator costs and timelines with `--profile-engine pytorch`.
- [Nsight Systems](https://docs.nvidia.com/nsight-systems/UserGuide/index.html): inspect work submission, CUDA calls, communication, and execution overlap on NVIDIA with `--profile-engine nsight`.
- [mcTracer](https://developer.metax-tech.com/doc?category_ids=61): capture execution timelines on MetaX devices with `--profile-engine mctracer`.

See the [profiling guide](../../../benchmarks/docs/profile/README.md) for preparation, capture, and viewing. After running `foretoken profile view`, inspect PyTorch traces and compatible mcTracer JSON in Perfetto, and Nsight timelines in the NVIDIA viewer.

When capturing:

- Start with a few representative samples and bound the window with `--profile-duration`. For concurrent or mixed workloads, retain the batch characteristics needed for the investigation while shortening the observation window.
- Capture steady-state execution after model loading, compilation, graph capture, and warmup. Analyze startup costs separately using startup logs. Confirm that the trace covers the phase of interest.
- Repeated inputs may hit the prefix cache. Choose separate workloads for cold prefill and cache reuse. Record generation settings and graph execution mode to explain differences.
- Profiling also adds overhead, so collect only the events and time window needed for the question. Compare latency and throughput in separate runs with profiling and temporary instrumentation disabled, keeping workload, warmup and cache conditions the same. If diagnosis required disabling CUDA Graph or adding synchronization, restore the intended deployment settings first and label the diagnostic results separately.
- Match multi-GPU data by process, rank, device, and the same unit of work, accounting for clock skew across nodes. Inspect compute/communication overlap and overlap between streams before judging the impact of a cost.
- For large traces, inspect file size and an event overview first, then filter by interval, process, or operator. Preserve the original file and extract relevant statistics and slices through a viewer or query tool.

## Follow hotspots into the source

Use event and operator names to locate the actual implementation. Read its input sizes, data layout, call frequency, and surrounding dependencies, then investigate how the cost arises.

- Expensive operators: analyze compute, memory access, and parallelism for the actual shapes, and compare how existing implementations partition work.
- Many short operators or calls: inspect repeated computation, format conversion, allocation, and submission for opportunities to combine work or reuse data.
- GPU gaps: correlate CPU submission and synchronization events to determine what the next operation is waiting for.
- Long communication: compare preceding computation, collective entry times, message sizes, and topology across ranks. Distinguish load imbalance and synchronization waits from transfer costs.
- Workload-specific regressions: use actual batch sizes, KV lengths, tokens per expert, and cache state to explain why the implementation slows down under those conditions.

## Continue with focused experiments

For a hotspot worth investigating further, extract real input shapes, data types, layouts, and state. Reuse upstream tests or write a small experiment for the current question. Retain adjacent operations, synchronization, or loop state where needed to explain the behavior of the actual execution.

Focused experiments can compare implementations, inspect intermediate results, locate numerical differences, or explain why a work partition is faster. When no ready-made script exists, construct inputs and reference results along the actual call path and build the experiment incrementally.

Make findings concrete: identify the implementation responsible for a cost, the conditions that trigger it, and a change that could improve it. Preserve input construction, measurement methods, and results for subsequent design work. After integrating a candidate into the service, confirm its effect under the target workload.

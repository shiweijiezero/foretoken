<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Performance profiling

[Observability](README.md) | [简体中文](profiling_zh.md)

Foretoken does not manage a profiling workflow. Use the metrics and dashboard first to identify a reproducible bottleneck, then run a controlled profile through the model runtime and hardware platform.

## Choose a profiling tool

- **PyTorch Profiler** identifies operator, CPU, CUDA, and memory activity in a model-server process.
- **Nsight Systems** shows host, device, and communication timelines across the serving process and GPU.
- **Nsight Compute** provides detailed kernel-level analysis for a focused CUDA workload.

Use the profiler entry point supported by the model-server image and the hardware platform. Foretoken does not add a common profiling flag or change the runtime command for these tools.

## Run a reproducible investigation

1. Select one model, replica, hardware shape, and runtime configuration.
2. Use a controlled request workload with a fixed concurrency, prompt distribution, and output limit.
3. Capture a short warm-up period separately from the measured profile.
4. Profile only the interval needed to answer the bottleneck question; profiling adds overhead and can change serving behavior.
5. Compare the profile with Frontend, model-server, accelerator, and RuntimeCache signals from the [Observability](README.md) dashboard.

Record the model identifier, Foretoken and backend image versions, Kubernetes resource requests, GPU type, parallelism, request workload, and profiler version with the result. Store profile artifacts in the platform's experiment or object storage, not in the repository.

## Interpret and clean up

Use the profile to distinguish compute saturation, communication delays, CPU scheduling, model loading, and cache or filesystem effects. Confirm any proposed change with the same workload and metrics before applying it to a serving deployment.

Profiling sessions, temporary debug settings, and captured traces belong to the operator who created them. Remove them through the runtime or platform workflow after the investigation; `foretoken uninstall` does not manage profiling artifacts or external profiler configuration.

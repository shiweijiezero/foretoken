<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Capture an NVIDIA Nsight Systems timeline

English | [简体中文](README_zh.md)

This Quick Start overlay prepares Qwen3-0.6B for CUDA/NVTX capture in the `foretoken-nsight` namespace. It requires a source-installed platform, an NVIDIA GPU and the optional diagnostic image configured as described in [Profiling](../../observability/profiling.md#nsight-systems).

```bash
foretoken deploy examples/nsight --timeout 20m
foretoken profile examples/nsight --profile-engine nsight --profile-duration 15s
```

Send inference requests through the frontend while the command reports `Capturing`. The command does not generate traffic. After export, the model continues serving and can be profiled again. The printed RuntimeCache location contains `capture.nsys-rep`, a SQLite export and `manifest.json` for each runtime.

Copy the reports you need before removing the deployment:

```bash
foretoken delete examples/nsight
```

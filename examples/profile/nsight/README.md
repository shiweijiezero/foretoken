<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Capture an NVIDIA Nsight Systems timeline

English | [简体中文](README_zh.md)

This Quick Start overlay captures CUDA/NVTX activity from Qwen3-0.6B in the `foretoken-nsight` namespace. It requires an NVIDIA GPU and the diagnostic image described in [Profiling](../../../benchmarks/docs/profile/README.md#nsight-systems). It shares the Quick Start's [model storage](../../../docs/model-storage.md): use the repository's `data/` mount with local k3d, or configure a node directory or PVC for a remote cluster.

After the [shared setup](../../../benchmarks/README.md#get-started), run a short workload from the repository root:

```bash
foretoken perf examples/profile/nsight \
  --profile --profile-engine nsight --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local,wandb
foretoken profile view
```

Select the Nsight timeline and click “Open in NVIDIA Nsight Systems” to inspect it in the official browser viewer. Capture files remain available after temporary benchmark workloads are removed.

Delete the retained example resources when no longer needed:

```bash
foretoken delete examples/profile/nsight
```

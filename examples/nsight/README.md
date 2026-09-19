<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Capture an NVIDIA Nsight Systems timeline

English | [简体中文](README_zh.md)

This Quick Start overlay captures CUDA/NVTX activity from Qwen3-0.6B in the `foretoken-nsight` namespace. It requires an NVIDIA GPU and the diagnostic image described in [Profiling](../../observability/profiling.md#nsight-systems). It shares the Quick Start's [model storage](../../docs/model-storage.md): use the repository's `data/` mount with local k3d, or configure a node directory or PVC for a remote cluster.

Run a short workload and capture it:

```bash
pip install -e '.[bench]'
foretoken bench examples/nsight \
  --profile --profile-engine nsight --profile-duration 15s \
  --number 2 --max-tokens 128 --output local
foretoken profile view
```

Download the report from the viewer and open it in Nsight Systems. Capture files remain available after temporary benchmark workloads are removed.

Delete the retained example resources when no longer needed:

```bash
foretoken delete examples/nsight
```

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Performance profiling

English | [简体中文](profiling_zh.md)

Use `foretoken bench --profile` to capture PyTorch CPU/GPU traces during a short inference workload. The command warms up the same request client, starts the profiler on the selected model's serving Pods, sends the capture workload, and retrieves the traces locally. Profiling adds overhead; its measurements are diagnostic results, not normal throughput comparisons.

## Capture a workload

The cluster must run a vLLM runtime with Torch profiling support. The operator needs Kubernetes permission to inspect ModelServices, ModelPools, ModelGroups and Pods, open Pod port-forwards, and copy files from model-server containers. The runtime image must provide `tar` for `kubectl cp`.

Enable profiling using the maintained platform configuration, then deploy the model:

```bash
foretoken install --values examples/profiling/platform-values.yaml
foretoken deploy examples/quickstart
foretoken bench examples/quickstart \
  --profile --warmup-requests 8 --number 4 \
  --parallel 2 --prompt "Explain why the sky is blue" --max-tokens 64
```

Use the existing source installation options when testing locally built images. Enabling profiling changes the runtime configuration; allow the model Pods to finish their rollout before capture. No profiler runs until `--profile` starts a session.

`--warmup-requests` counts requests dispatched before capture starts; these responses do not enter the recorded request metrics. Warm-up requests still in flight may appear in the trace. `--number` counts requests submitted during capture. Both phases use the same client, concurrency queue, generation parameters, and arrival-rate clock. Use `--warmup-requests 0` to capture a cold request.

A deployment with multiple models requires `--model`. Capture runs support a single prompt or dataset source and the normal `--parallel`, `--rate`, and `--open-loop` load controls. Sweeps, trace replay, multiple dataset sources, and SLA tuning are separate benchmark modes. `--url` is not supported for profiling because it does not identify the model Pods or provide an artifact-retrieval path.

## Read the results

Results are written below `results/profiles/<session-id>/`, or the directory selected by `--output-dir`. Each Pod has its own trace files; `profile-run.json` records the model, request configuration, runtime image, warm-up count, capture metrics, and collection status. Profile mode never sends results to W&B or enters Pareto analysis, regardless of ordinary benchmark output defaults.

Open the generated `.pt.trace.json` or `.pt.trace.json.gz` files in a local Perfetto viewer or another PyTorch-compatible trace viewer. Compare CPU scheduling, CUDA kernels and communication with the [service metrics](README.md). Nsight Systems and Nsight Compute remain separate hardware-level tools, not alternative backends for this command.

## Failures and cleanup

Each model-server permits one profiler session at a time without changing inference admission. Request failure or Ctrl+C settles the benchmark's in-flight requests, stops all captures started by that command, and attempts to collect their artifacts. A server-side deadline derived from the workload bounds abandoned sessions if the benchmark process disappears.

Successful collection removes only that session's files from the Pods and closes its port-forwards. Failed collection leaves files under `/tmp/foretoken-profiles/<session-id>/` for recovery; the error identifies the affected Pod. Use the normal Kubernetes file-copy workflow to retrieve them before deleting the stopped session through its internal management endpoint. A Pod restart removes these temporary files. Do not roll out or scale the selected model during a capture.

Disable the runtime profiling option and repeat `foretoken install` when profiling access is no longer needed. Profiling endpoints exist only on the restricted model-server API, not the public inference frontend.

References: [vLLM profiling](https://docs.vllm.ai/en/latest/contributing/profiling/) and [PyTorch Profiler](https://docs.pytorch.org/tutorials/recipes/recipes/profiler_recipe.html).

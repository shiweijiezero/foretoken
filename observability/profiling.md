<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Profile an existing service

English | [简体中文](profiling_zh.md)

Collect a short PyTorch CPU/GPU timeline while a prepared model service handles requests. This experimental feature requires a source installation and currently supports the vLLM PyTorch profiler on NVIDIA GPUs.

## Capture

Use the Kustomize directory that identifies your existing diagnostic deployment:

```bash
foretoken profile examples/quickstart \
  --profile-engine pytorch \
  --profile-duration 15s
```

The command reads the directory without applying it. If it contains several models, select one with `--model MODEL_ID`. It does not generate traffic: send requests through the normal frontend while capture is running.

The runtime stops recording after the requested duration, then exports the files. Export may take longer than recording. Normal completion leaves the model serving. The command prints a ProfileRun name for later inspection and a persistent volume claim (PVC) and path for the results; it does not download them.

| Option | Meaning |
|---|---|
| `--profile-engine pytorch` | Required profiler selection; only PyTorch is available |
| `--profile-duration 15s` | Required recording duration; excludes startup and export |
| `--model MODEL_ID` | Select one model from a multi-model directory |
| `--timeout 10m` | How long the CLI observes the run, not how long the runtime records |

Ctrl-C requests cancellation and retains available output. A lost terminal or an observation timeout does not cancel an accepted run: the runtime still stops automatically. Do not submit another capture just because the first command stopped waiting. Use its printed inspection command to check the original run.

## Prepare the deployment

Preparation happens before model deployment, not during each capture:

1. Choose a diagnostic namespace and create a dedicated artifact PVC there. All participating model-server Pods need write access. Use ReadWriteMany storage for Pods on different nodes, and keep this claim separate from model cache and KV storage.
2. Copy [the profiling values example](../deploy/profiling-values.example.yaml), replacing its namespace and claim with yours. Pass the file to a [source installation](../docs/custom-deployment.md) with `foretoken install -e . --values YOUR_VALUES_FILE`. The source builds the controller, CRDs and model-server together. Use the registry option required by your cluster.
3. Deploy the model into that namespace and wait for readiness. The caller needs Kubernetes permissions to create, read and patch ProfileRuns. The CLI uses the active Kubernetes context.

The binding prepares all model-server Pods in that namespace and changes their deployment templates. Use a dedicated diagnostic namespace. Capturing never patches or restarts Pods to install a missing profiler. Monitoring and Alertmanager are not prerequisites.

## Inspect results

Each run retains native `.pt.trace.json` files and a manifest on the artifact PVC. Access the files through your platform's storage access and inspect them with a local Perfetto viewer or another compatible trace viewer. The manifest separates the recording stop request from completed export; neither includes client-side file transfer.

A valid capture with no GPU kernel activity is reported as idle, not as evidence of a healthy or busy GPU. Missing worker files or malformed traces fail publication. Cancellation may retain incomplete output. Multiple captures have separate result directories.

Profiling adds CPU/GPU overhead and can produce large files even in a short window. This command captures the selected service's prepared serving runtimes, not a request-count sample. Use a small diagnostic deployment and short duration; it does not cap GPU events or artifact bytes. A native profiler failure may terminate the diagnostic runtime, so use a service where interruption is acceptable. Deleting its namespace or artifact PVC may delete the retained results.

Deployment-triggered capture, benchmark integration, delay, request sampling, Nsight Systems and MetaX tools are not part of this single-capture interface. See the [maintainer design and delivery plan](../docs/development/profiling.md) for their boundaries.

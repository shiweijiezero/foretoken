<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Service-owned profiling

English | [简体中文](profiling_zh.md)

The experimental implementation captures one time-bounded PyTorch or Nsight Systems window on an existing diagnostic ModelService. It is independent of benchmark execution and monitoring. Start with the [operator guide](../../observability/profiling.md) for the command and result access.

## Ownership and execution

A capture must stop and retain results even when its initiating command disconnects. Its identity and lifetime belong to a namespaced `ProfileRun`, not to the command process or `ModelService.spec`.

ProfileRun operations use Kubernetes RBAC. Internal HTTP follows the existing platform network trust boundary.

## Prepare before capture

Profiling uses the ModelService's resolved RuntimeCache binding. The ModelGroup controller mounts its PVC as the data root, and the runtime derives `profiles/` below that root. Profiling does not use KV offload or connector volumes, and ProfileRun does not own workloads or storage.

A serving Group without a persistent RuntimeCache cannot participate in capture. The controller reports this before starting native work, while a runtime that has fallen back to Pod-local temporary cache storage reports profiling unavailable. Neither case creates another volume or redirects results to temporary storage.

The model-server image includes PyTorch capture support. The [vLLM backport](../../data-plane/patches/vllm-python-profiling.patch) reports native start/stop failures and allows subsequent captures. Profiling begins only when a capture is requested.

`ModelService.spec.profiling.engine` selects process preparation, defaulting to PyTorch when omitted. The compiler carries it through the pool and group into the launch plan. The resolver selects `runtime.vllm.nsightImage` only for Nsight services and requires NVIDIA GPUs and persistent RuntimeCache storage. Changing preparation replaces the engine processes; ProfileRun owns each subsequent recording window.

The Nsight adapter wraps the managed engine command with `nsys launch` and a runtime-specific session. It prepares CUDA/NVTX tracing for the engine process tree, using spawned vLLM workers and CUDA graph node tracing. The existing managed engine handle owns process monitoring and shutdown. `nsys start` begins recording; `nsys stop` exports the native report while leaving the application running. CPU sampling and context-switch collection are disabled. PyTorch instrumentation is disabled for this process, so the two profilers do not compete for CUPTI.

Runtime observations identify the prepared profiler, and the controller rejects a mismatched ProfileRun before capture. Missing engine fields retain the original PyTorch wire behavior so existing runtimes remain compatible.

## Identity and recovery

The API fixes target, engine and duration at creation. Actions move from `Capture` to `Finish` or `Cancel`; cancellation cannot be reversed. The Kubernetes UID distinguishes runs even when a resource name is reused.

Before native work starts, reconciliation persists a deletion finalizer and an execution plan in ProfileRun status. The plan fixes the serving generation, RuntimeCache claim, Groups, Pods and runtime participants. Controller restart reads the same plan rather than selecting replacement instances or storage.

Status progresses through `Starting`, `Capturing` and `Stopping` to `Succeeded`, `Failed` or `Cancelled`. All selected participants must be capturing before the run reports `Capturing`; success requires every expected participant's result. Sending an HTTP operation alone is not completion. The runtime rejects competing captures and handles same-run retries without restarting or extending the window.

An unreachable or replaced participant or changed serving cohort causes cancellation of remaining work and a coverage or stop error. A missing Pod is not proof that its engine stopped. Unconfirmed stop retains the finalizer and plan for operator diagnosis. Deleting a live run also requests cancellation; sealed artifacts are not owned by the run and are not garbage-collected with it.

## Runtime deadlines

Native utilities run in supervisor-owned tasks independently of HTTP request lifetime. Cancellation changes the desired action; it does not abandon an in-flight utility.

The caller specifies the duration; the runtime starts that timer after native start succeeds. Native start and stop/flush have separate 30-second and 120-second budgets. Early `Finish` stops a shorter window. The CLI's default 10-minute observation timeout changes none of these deadlines.

Without an active capture, the runtime retains its normal request-drain and shutdown order. A failed or timed-out utility leaves native profiler state uncertain. The diagnostic runtime closes admission and follows its existing engine process-group shutdown path before releasing the utility task. This can interrupt inference on that service. Failure to confirm termination is reported rather than represented as success. Partial output remains for storage-owner diagnosis.

## Seal before publication

Each runtime writes below the persistent data root supplied by its RuntimeCache PVC:

```text
<data-root>/profiles/.staging/<runtime-id>/
<data-root>/profiles/runs/<run-uid>/<runtime-id>/
```

Before starting, staging must be empty; unhandled output is not erased. After native stop/flush, PyTorch success requires one valid trace per expected worker. Nsight success requires a native report that NVIDIA's exporter can read; the retained SQLite export supplies the GPU activity flag from kernel events. Nsight reports cover the instrumented process tree rather than one file per worker. GPU activity is reported separately, and a valid idle window does not fail publication. Cancellation retains available output without claiming completeness.

After stop/flush, the supervisor writes the manifest and atomically renames the staging directory on the same filesystem before publishing the artifact reference. Atomicity is per participant, not a distributed transaction. Later captures use a different run path. Publication failure neither produces success nor deletes recoverable data.

The manifest's `startedAtUnixMs` follows native start, `recordingEndedAtUnixMs` marks the stop request, and `exportedAtUnixMs` follows stop/flush. Native workers may stop at slightly different times; these control timestamps do not claim exact GPU event boundaries. ProfileRun `finishedAt` is controller-observed completion.

## Upstream references

- [Nsight Systems](https://docs.nvidia.com/nsight-systems/UserGuide/index.html): interactive sessions, tracing and native report export.

- [vLLM profiling](https://docs.vllm.ai/en/stable/contributing/profiling/): engine setup, output and diagnostic overhead.
- [PyTorch profiler](https://docs.pytorch.org/docs/stable/profiler.html): native capture and trace export.

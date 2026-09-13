<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Service-owned profiling

English | [简体中文](profiling_zh.md)

The experimental implementation captures one time-bounded Torch window on an existing diagnostic ModelService. It is independent of benchmark execution and monitoring. Start with the [operator guide](../../observability/profiling.md) for the command and result access.

## Ownership and execution

A capture must stop and retain results even when its initiating command disconnects. Its identity and lifetime belong to a namespaced `ProfileRun`, not to the command process or `ModelService.spec`.

The command does not generate traffic, change serving configuration, deploy a benchmark Job, open a public profiling port, or copy files out of Pods. An inference token permits requests, not Kubernetes profiling control. ProfileRun operations use Kubernetes RBAC; internal HTTP follows the existing platform network trust boundary, not per-user authorization between Pods.

## Prepare before capture

The ModelService's resolved RuntimeCache binding is the only persistent storage binding used by profiling. The ModelGroup controller already mounts its PVC as the data root and projects the claim, Pod and Group identities required by model-server. The runtime derives `profiles/` below that root; it never uses KV offload or connector volumes. ProfileRun does not own or mutate workloads or storage.

A serving Group without a persistent RuntimeCache cannot participate in capture. The controller reports this before starting native work, while a runtime that has fallen back to Pod-local temporary cache storage reports profiling unavailable. Neither case creates another volume or redirects results to temporary storage.

The model-server image includes PyTorch capture support. The [vLLM backport](../../data-plane/patches/vllm-python-profiling.patch) reports start/stop failures at the native control boundary and recreates completed Torch state before subsequent captures. Runtime startup prepares process identity and private staging; profiling begins only when a capture is requested.

## Identity and recovery

The API fixes target, engine and duration at creation. Actions move from `Capture` to `Finish` or `Cancel`; cancellation cannot be reversed. The Kubernetes UID distinguishes runs even when a resource name is reused.

Before native work starts, reconciliation persists a deletion finalizer and a typed execution plan in ProfileRun status. It resolves the ModelService's committed serving generation with existing routing helpers and checks the Pod → ReplicaSet → Deployment → ModelGroup ownership chain. The plan stores the fixed RuntimeCache claim together with service, Group, Pod and runtime identities. Controller restart reads the same plan rather than selecting replacement instances or storage.

Status progresses through `Starting`, `Capturing` and `Stopping` to `Succeeded`, `Failed` or `Cancelled`. All selected participants must be capturing before the run reports `Capturing`; success requires every expected participant's result. Sending an HTTP operation alone is not completion. The runtime rejects competing captures and handles same-run retries without restarting or extending the window.

An unreachable or replaced participant or changed serving cohort causes cancellation of remaining work and a coverage or stop error. A missing Pod is not proof that its engine stopped. Unconfirmed stop retains the finalizer and plan for operator diagnosis. Deleting a live run also requests cancellation; sealed artifacts are not owned by the run and are not garbage-collected with it.

## Runtime deadlines

State updates hold a short lock. Native utilities run in tasks owned by the supervisor, outside that lock and independently of HTTP request tasks. Cancellation changes desired action; it does not abandon an in-flight utility.

The caller specifies the duration; the runtime starts that timer after native start succeeds. Native start and stop/flush have separate 30-second and 120-second budgets. Early `Finish` stops a shorter window. The CLI's default 10-minute observation timeout changes none of these deadlines.

Without an active capture, the runtime retains its normal request-drain and shutdown order. A failed or timed-out utility leaves native profiler state uncertain. The diagnostic runtime closes admission and follows its existing engine process-group shutdown path before releasing the utility task. This can interrupt inference on that service. Failure to confirm termination is reported rather than represented as success. Partial output remains for storage-owner diagnosis.

## Seal before publication

Each runtime writes below the persistent data root supplied by its RuntimeCache PVC:

```text
<data-root>/profiles/.staging/<runtime-id>/
<data-root>/profiles/runs/<run-uid>/<runtime-id>/
```

Before starting, staging must be empty; unhandled output is not erased. After native stop/flush, success requires one valid Torch trace per expected worker. GPU activity is reported separately; a valid idle window does not fail publication. Validation streams events rather than loading the whole trace into memory. Cancellation retains available output without claiming completeness.

The supervisor writes and flushes the manifest, renames the whole staging directory on the same filesystem, and flushes destination directories before publishing the artifact reference. Atomicity is per participant, not a distributed transaction. Later captures recreate staging and use a different run path. Publication failure neither produces success nor deletes recoverable data.

The manifest's `startedAtUnixMs` follows native start, `recordingEndedAtUnixMs` marks the stop request, and `exportedAtUnixMs` follows stop/flush. Native workers may stop at slightly different times; these control timestamps do not claim exact GPU event boundaries. ProfileRun `finishedAt` is controller-observed completion. The command returns the RuntimeCache PVC and a `profiles/runs/...` path, not a workstation download. Retention follows the RuntimeCache storage lifecycle.

## Planned command recipes

These recipes map to the delivery steps below. See the [operator guide](../../observability/profiling.md) for current capture commands. All entrypoints share option names.

```bash
# Capture after deployment readiness and an initial delay
foretoken deploy examples/quickstart --profile \
  --profile-delay 30s --profile-duration 15s --profile-engine pytorch

# Capture during a benchmark
foretoken bench examples/quickstart --dataset random --profile \
  --profile-duration 15s --profile-engine pytorch

# Sample request paths under existing traffic
foretoken profile examples/quickstart --profile-duration 30s \
  --profile-request-sampling 0.01 --profile-request-limit 100

# NVIDIA Nsight Systems
foretoken profile examples/quickstart --profile-duration 5s --profile-engine nsight

# MetaX mcTracer
foretoken profile examples/quickstart --profile-duration 5s --profile-engine mctracer
```

## Incremental delivery

Each step is a separately usable and validated PR, not a horizontal split between CLI and runtime:

1. Existing-service single PyTorch capture: this implementation, with explicit `--profile-engine pytorch` and `--profile-duration`.
2. Server-owned delayed start through `--profile-delay`, including cancellation while waiting.
3. `deploy --profile`, reusing the capture client after readiness without deleting the service afterward.
4. `bench --profile`, integrated with the existing executor's actual dispatch events rather than a second load generator.
5. Request-path observation together with bounded request sampling and a service-wide request limit. Only then can omission of `--profile-engine` mean request-only capture.
6. NVIDIA Nsight Systems via `--profile-engine nsight`, with report finalization while serving remains running.
7. MetaX mcTracer via `--profile-engine mctracer`, validated against its actual noninteractive control and export capabilities.

All entrypoints share the same `--profile-*` names as their capabilities ship. Engine-step, repeated-window and gap options are deferred. Matching model images provide the capture tools, deployment resolves persistent RuntimeCache storage, and each capture selects its recording window.

Acceptance must use actual engine traces and verify repeated independent runs, idle capture, cancellation, CLI loss, controller recovery, native failure and artifact publication failures. Multi-worker coverage and overhead need their own hardware evidence; a successful single-worker run does not establish them.

## Upstream references

- [vLLM profiling](https://docs.vllm.ai/en/stable/contributing/profiling/): engine setup, output and diagnostic overhead.
- [PyTorch profiler](https://docs.pytorch.org/docs/stable/profiler.html): native capture and trace export.
- [Dynamo Profiler](https://docs.dynamo.nvidia.com/dynamo/dev/knowledge-base/modular-components/profiler/overview): deployment characterization has different outputs and ownership from bounded runtime trace capture.

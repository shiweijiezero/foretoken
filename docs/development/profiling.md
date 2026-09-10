<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Service-owned profiling

English | [简体中文](profiling_zh.md)

The experimental implementation captures one time-bounded Torch window on an existing diagnostic ModelService. It is independent of benchmark execution and monitoring. Start with the [operator guide](../../observability/README.md#one-off-profiling-experimental-source-build) for preparation, the command and artifact access.

## Ownership and execution

A capture must stop and retain results even when its initiating command disconnects. Its identity and lifetime belong to a namespaced `ProfileRun`, not to the command process or `ModelService.spec`.

```text
ordinary workload ── public frontend ──> model-server
                                            │
foretoken profile ── Kubernetes API          │
                          │                 │
                      ProfileRun            │
                          │                 │
                 existing controller ────────┘
                    internal HTTP           │
                                     runtime supervisor
                                            │
                                      vLLM / Torch
                                            │
                                      artifact PVC
```

The CLI creates and observes the run; Ctrl-C requests cancellation. The existing control-plane manager selects the serving cohort, sends intent through model-server's existing internal listener, and publishes observed status. The runtime supervisor owns native start, automatic stop, export and failure handling. The platform owns the dedicated artifact PVC and retention.

The command does not generate traffic, change serving configuration, deploy a benchmark Job, open a public profiling port, or copy files out of Pods. An inference token permits requests, not Kubernetes profiling control. ProfileRun operations use Kubernetes RBAC; internal HTTP follows the existing platform network trust boundary, not per-user authorization between Pods.

## Prepare before capture

The platform's `profiling.artifactClaims` maps a diagnostic namespace to an existing PVC. The ModelGroup controller projects the mount and runtime identity into its normal Pod template. This changes workloads at deployment time, so configure it before deploying diagnostic services. ProfileRun does not own or mutate workloads or PVCs.

At startup, the runtime creates a fresh process identity and configures the installed engine's Torch profiler to write uncompressed traces into private staging. The model-server image applies the [vLLM Python backport](../../data-plane/patches/vllm-python-profiling.patch) for error propagation and independent subsequent captures. The Rust source submodule is not the installed Python engine. An image must accept the backport or already contain that exact implementation.

## Identity and recovery

The CLI submits this API intent; users do not write this YAML for each capture:

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: ProfileRun
metadata:
  generateName: profile-
  namespace: foretoken-diagnostic
spec:
  modelServiceRef:
    name: diagnostic-model
  duration: 15s
  action: Capture
```

The API fixes target and duration at creation. Actions move from `Capture` to `Finish` or `Cancel`; cancellation cannot be reversed. The Kubernetes UID distinguishes runs even when a resource name is reused.

Before native work starts, reconciliation persists a deletion finalizer and an immutable, run-owned ConfigMap execution plan. It resolves the ModelService's committed serving generation with existing routing helpers and checks the Pod → ReplicaSet → Deployment → ModelGroup ownership chain. The plan stores fixed service, group, Pod and runtime identities. Controller restart reads the same plan rather than selecting replacement instances.

Status progresses through `Starting`, `Capturing` and `Stopping` to `Succeeded`, `Failed` or `Cancelled`. All selected participants must be capturing before the run reports `Capturing`; success requires every expected participant's result. Sending an HTTP operation alone is not completion. The runtime rejects competing captures and handles same-run retries without restarting or extending the window.

An unreachable or replaced participant or changed serving cohort causes cancellation of remaining work and a coverage or stop error. A missing Pod is not proof that its engine stopped. Unconfirmed stop retains the finalizer and plan for operator diagnosis. Deleting a live run also requests cancellation; sealed artifacts are not owned by the run and are not garbage-collected with it.

## Runtime deadlines

State updates hold a short lock. Native utilities run in tasks owned by the supervisor, outside that lock and independently of HTTP request tasks. Cancellation changes desired action; it does not abandon an in-flight utility.

The ProfileRun API owns the default 15-second duration; the runtime starts that timer after native start succeeds. Native start and stop/flush have separate 30-second and 120-second budgets. Early `Finish` stops a shorter window. The CLI's default 10-minute observation timeout changes none of these deadlines.

A failed or timed-out utility leaves native profiler state uncertain. The diagnostic runtime closes admission and follows its existing engine process-group shutdown path before releasing the utility task. This can interrupt inference on that service. Failure to confirm termination is reported rather than represented as success. Partial output remains for storage-owner diagnosis.

## Seal before publication

Each runtime writes to a dedicated, shared-writable artifact PVC, separate from runtime cache and KV storage:

```text
<artifact-volume>/.staging/<runtime-id>/
<artifact-volume>/runs/<run-uid>/<runtime-id>/
```

Before starting, staging must be empty; unhandled output is not erased. After native stop/flush, success requires one valid Torch trace containing GPU kernel activity per expected worker. Validation streams events rather than loading the whole trace into memory. Cancellation retains available output without claiming completeness.

The supervisor writes and flushes the manifest, renames the whole staging directory on the same filesystem, and flushes destination directories before publishing the artifact reference. Atomicity is per participant, not a distributed transaction. Later captures recreate staging and use a different run path. Publication failure neither produces success nor deletes recoverable data.

The manifest's `startedAtUnixMs` follows native start; `stoppedAtUnixMs` follows stop/flush, so their difference includes export and is not pure recording duration. ProfileRun `finishedAt` is controller-observed completion. The command returns a PVC/path reference, not a workstation download. Namespace or PVC deletion remains a storage-owner operation and can remove artifacts.

## Validation scope

Real Kubernetes acceptance covered a single-worker NVIDIA A100 service with vLLM 0.26.0: two separate captures, Ctrl-C cancellation with retained output, automatic completion after CLI termination, controller restart recovery, and usable inference afterward. Perfetto parsed the traces with GPU kernel counts matching the original JSON; CPU slice-overlap import diagnostics remain and were not hidden by modifying files.

Native-utility hangs, failed storage publication, multi-worker coverage and profiling overhead still require hardware measurement. Direct lifecycle checks do not replace those experiments. Benchmark integration, delay, sampling limits, repeated windows, Nsight and MetaX are outside this implementation; none is required to use the independent command.

## Upstream references

- [vLLM profiling](https://docs.vllm.ai/en/stable/contributing/profiling/): engine setup, output and diagnostic overhead.
- [PyTorch profiler](https://docs.pytorch.org/docs/stable/profiler.html): native capture and trace export.
- [Dynamo Profiler](https://docs.dynamo.nvidia.com/dynamo/dev/knowledge-base/modular-components/profiler/overview): deployment characterization has different outputs and ownership from bounded runtime trace capture.

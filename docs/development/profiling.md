<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# On-demand profiling design

English | [简体中文](profiling_zh.md)

**Status: target design with a local single-window prototype.** `bench --profile`, delay, duration, runtime cancellation/status, and exec-based retrieval are implemented locally; GPU integration is not yet validated. Standalone `profile`, repeat/interval, sample limits, and additional backends below remain proposals, not released interfaces. Current commands are documented in the [benchmark guide](../../benchmarks/README.md#on-demand-profiling).

## 1. Two entry points, one capture capability

Profiling records model execution while requests are being processed. It is a finite, one-off operation, independent of model deployment, Prometheus, Grafana, and alerting.

- `foretoken bench PATH --profile`: the existing benchmark executor supplies requests while short capture windows run. It continues to own request content, concurrency, and rate.
- `foretoken profile`: select an existing Foretoken service and capture its current traffic, without silently generating additional requests. Service selection and authorization syntax remain open in section 6.

Both submit the same capture plan. Users do not edit examples, Helm charts, or observability YAML, discover Pods, forward ports, or issue start/stop HTTP requests. Images and runtimes prepare profiler support internally; being ready to profile does not mean continuously recording.

## 2. Bound collection with small windows

These are candidate options and semantics shared by both entry points. An unresolved unit must not become a public field prematurely.

| Choice | Candidate option | Meaning |
| --- | --- | --- |
| Tool | `--profile` for bench; standalone selector TBD | Torch, Nsight Systems, or the MetaX native tool; expose only validated implementations |
| Initial delay | `--profile-delay` | Seconds from plan activation until the first window, not from model deployment |
| Window length | `--profile-duration` | Target collection seconds per window, after which stop is requested; export time is separate |
| Window count | `--profile-repeat` | Finite total number of windows, including the first |
| Gap | `--profile-interval` | Wait after the preceding window has stopped and exported, before starting the next; not a fixed start-to-start frequency |
| Sample cap per window | Name and unit TBD | Stop when either the cap or duration is reached; requests, engine steps, and tool samples are not interchangeable |

For example, wait 30 seconds, collect for 5 seconds, wait 20 seconds after export, and repeat for 3 windows in total. These values illustrate semantics, not defaults. The default should be one short capture; determine its duration through a real small-workload check rather than defaulting to indefinite collection.

A window still ends on time without incoming requests and reports that no inference work was captured. Do not extend it or generate traffic implicitly. Report actual capture start and end times. Instances execute independently; do not promise precisely synchronized starts across Pods.

vLLM's `delay_iterations` / `max_iterations` and the [PyTorch schedule](https://docs.pytorch.org/docs/2.14/profiler.html#torch.profiler.profiler.schedule) count execution steps, not seconds. An engine step is not one user request. If samples mean requests, define the start/completion counting point and batching/prefill/decode semantics first; `bench --number` cannot stand in for exact server-side request selection.

## 3. Submit locally; time and execute inside the cluster

The workstation selects a service, submits the complete plan, and retrieves results. The model-server's existing process-management path owns window execution rather than relying on timed start/stop calls from the workstation.

```text
bench --profile --+
                 +-- 1. shared submission -- 2. Kubernetes management channel
profile service -+                                      |
                                                        v
                         3. model-server: delay -> capture -> export -> gap
                                                        |            `-- finite repeat
                                                        v
                         4. backend adapter -> profiler inside the engine
                                                        |
                                                        v
                         5. save window results -> automatic retrieval and report
```

The local single-window implementation uses Kubernetes Pod exec: invoke the existing model-server management port from inside its container and hand the window to an in-process task. Exec submits or queries; it does not host the timing loop. Users do not execute these internal commands. This adds no TCP listener, Service, Gateway route, or workstation port-forward.

Participants come from the selected service's committed serving generation, restricting operations to that Foretoken service. Kubernetes RBAC protects exec calls. Management handlers separately require a loopback peer; they do not trust forwarded-address headers. CPU checks cover these handlers, but the deployed listening configuration and Kubernetes permissions still need end-to-end validation.

The model-server owns the active task after the submitting request returns. At a window deadline it requests stop; export must complete before another window starts. Cancellation prevents later windows and requests termination of the current capture. Reject overlapping captures on one engine instead of replacing them. If some participants fail to start, cancel those started for this run and report partial results.

A disconnected workstation must not leave a submitted window recording indefinitely. Native stop/export failure or a stuck operation must be reported as failed or not confirmed stopped; an expired timer is not proof of termination. Do not kill or restart an existing service by default for a diagnostic operation. Pod restart does not resume an old plan or transfer it to replacement instances. No new CRD, reconciler, Job, or always-on profiling service is introduced.

Bench still performs one normal execution: prepare inputs, activate the plan as request execution begins, and end remaining captures when the workload finishes or is interrupted. Do not repeat benchmarks to fill the requested window count. Requests may run during the delay; do not wait until profiling has ended to generate traffic. Integrate with the existing executor's preparation/execution boundary, without a second client, scheduler, or warm-up request path.

Separate output by invocation, window, and runtime, retaining native backend files without overwriting consecutive captures. Shared submission code automatically retrieves completed files; users do not run `kubectl cp`, and no download port or dedicated PVC is required. Uncollected files follow the existing Pod storage lifetime, with no guarantee beyond Pod deletion. Benchmark-owned temporary services are cleaned up only after capture stops and results are handled; retrieval failures must preserve recoverable output and be reported rather than silently deleting it.

## 4. A common command does not imply identical backend startup

| Backend direction | Implementation or feasibility boundary |
| --- | --- |
| PyTorch Profiler | Reuse engine controls and export. Verify device events on NVIDIA and the MetaX PyTorch build separately; a CPU-only trace is not evidence of GPU support |
| Nsight Systems | Reuse `nsys` and native capture controls; verify attachment to existing services, multi-window export, and idle runtime overhead |
| MetaX native tool | Confirm the actual tool, version, lifecycle, and output interface before adapting it to the plan; do not invent a tool name or copy NVIDIA commands |

The [vLLM Torch and Nsight examples](https://docs.vllm.ai/en/latest/contributing/profiling/) demonstrate different prerequisites: the documented Nsight server starts under `nsys`. Foretoken images and startup integration own the no-user-YAML experience; it does not prove that any existing process can be instrumented without preparation. A capture command must not implicitly roll or restart an existing service. Report an unavailable selected backend before starting.

Learn engine controls and backend-specific parameters from [Dynamo Runtime Profiling](https://github.com/ai-dynamo/dynamo/blob/main/docs/fern/pages/developer-guide/knowledge-base/modular-components/profiler/profiler-guide.md#runtime-profiling), without importing deployment optimization, capacity characterization, or automatic deployment into short-window capture.

## 5. Ownership and implementation order

| Location | Responsibility |
| --- | --- |
| [`cli/foretoken/`](../../cli/foretoken/) | Shared service selection, submission, observation, and retrieval for both entry points, using existing Kubernetes facilities |
| [`benchmarks/`](../../benchmarks/) | Supply requests and integrate one execution with a capture plan; do not own capture timers or duplicate Pod lifecycle |
| [`data-plane/model-server/`](../../data-plane/model-server/) | In-process plans, controls, cancellation, and backend adapters, reusing process management and the engine client |
| [`deploy/`](../../deploy/) | Image tools and required runtime preparation, without user-facing profiling YAML switches |
| [`data-plane/third_party/vllm/`](../../data-plane/third_party/vllm/) | Only generally reusable upstream fixes, not Foretoken commands, timing, or Kubernetes behavior |

Validate the implemented delayed Torch window with a real workload, then connect the standalone entry point to the same control path. Extend it with gaps, finite repetition, and the agreed sample cap. Integrate Nsight and MetaX after validating their startup conditions separately. Use real small workloads to verify windows, export, repeated runs, cancellation, and continued ordinary inference; do not introduce permanent tests or CI as prerequisite infrastructure.

The local prototype replaces profiling port-forwarding, whole-benchmark capture, extra warm-up requests, and file-set differences with one runtime-owned window. `PUT/GET/DELETE /v1/internal/profile/{id}` on the existing listener accepts, observes, and cancels a client-generated ID; these handlers accept loopback peers only. The supervisor retains the active or latest result in memory; after a newer capture or process restart, an old ID is explicitly unavailable, not silently redirected. Native files retain their Pod storage lifetime. A failed native control blocks another capture until runtime recovery, without restarting the service. CLI retrieval failures retain benchmark-created resources. The model-server Dockerfile installs the repository-owned Python backport for per-capture names and export errors; it does not depend on uncommitted changes inside the Rust build's vLLM submodule. Engine-image requirements are documented in the [source image lifecycle guide](source-image-lifecycle.md). GPU integration remains unvalidated. Ordinary inference access is unchanged.

## 6. Two interface decisions before implementation

1. **Sample unit and scope:** requests, engine steps, or tool samples? Is the cap per window, per worker, or service-wide? Do not expose an ambiguous `--samples` until defined.
2. **Existing-service identity and authorization:** what does “service token” identify and authorize, and is there an existing lookup/control interface? Review a `ModelService + namespace` management entry first if appropriate, but do not equate an inference API key with Pod control or invent a token-triggered prefill protocol.

These decisions do not prevent defining shared timing and ownership, but they affect the final command syntax and acceptance criteria. Nsight/MetaX attachment requirements are backend feasibility work, not a reason to shift configuration back to user YAML.

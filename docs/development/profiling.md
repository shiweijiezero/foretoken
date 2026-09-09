<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Profiling execution design

English | [简体中文](profiling_zh.md)

**Status: proposed architecture; not an implemented user interface.** This document defines the replacement for the workstation-controlled profiling prototype. Monitoring and request tracing remain independent features.

## Purpose and user path

A profile explains where a running inference engine spends time under a known workload. It is a diagnostic capture, not a normal performance score. The workload generator should not become a Pod controller or a file-transfer service.

The proposed path uses a previously deployed diagnostic service:

```bash
foretoken deploy examples/quickstart
foretoken bench examples/quickstart --profile --profile-duration 15s
```

The platform must first enable the supported profiler and provision persistent artifact storage for that workload namespace. Those settings are applied before the service starts, not by changing its Pod during capture.

The first implementation targets one unambiguously selected, already Ready ModelService using the validated vLLM Torch adapter. It does not implicitly deploy or delete model services. A normal benchmark keeps its existing deployment behavior. URL-only profiling is not included because a public inference URL alone does not identify an authorized capture target.

## Keep benchmark execution unchanged

The current benchmark and its request-execution refactor run locally, not in a Kubernetes benchmark Job. The runner receives the public inference URL, model and workload configuration. In the refactored standard path, EvalScope owns the client, event loop, request scheduling and cancellation.

Profiling does not change that execution location. It adds a small orchestration boundary around **one** invocation of the standard benchmark executor:

1. Resolve the existing ModelService and public endpoint. Prepare configuration, dataset materialization, tokenizer inputs and adapter imports before capture; keep that preparation in the existing execution adapter without starting a second client or benchmark.
2. Create a `ProfileRun` and wait until its controller reports `Capturing`. A terminal state reached before this point ends the attempt rather than starting an unrelated workload.
3. Execute the standard benchmark once against the public endpoint.
4. Request `Finish` when the workload completes, or `Cancel` on interruption.
5. Read the terminal capture status and report its durable artifact reference.

The implementation must expose preparation and execution as two stages of the existing adapter; it must not assume EvalScope already provides such an interface. Record the executor's actual request interval and the runtime's capture interval. If unavoidable executor startup consumes the window and no workload overlaps it, the benchmark profile result is `NoWorkloadOverlap`, even if the capture produced a valid file. ProfileRun success describes capture completion, not benchmark success.

The profiler's duration is a maximum wall-clock capture window. An earlier `Finish` seals a shorter window; reaching the duration stops capture even if the benchmark is still running. Results record the actual capture interval. They do not claim that every request in the benchmark belongs to that interval, or enter normal W&B/Pareto comparison publication.

The first version does **not** expose the prototype's `--warmup-requests`. The standard EvalScope adapter currently has no supported phase hook through which Foretoken can coordinate an exact warm-up boundary. Two benchmark calls, an extra warm-up client, a copied scheduler, or monkeypatching EvalScope are not substitutes. Engine warm-up can be performed as a separate ordinary benchmark; it must not be described as same-client steady-state warm-up. A future exact phase interface belongs to the upstream executor and must distinguish conversations from HTTP turns.

## One capture lifecycle owner

```text
workstation benchmark ── public URL ──> Frontend ──> model-server
        │
        └── Kubernetes API ──> ProfileRun
                                  │
                         existing controller manager
                                  │ internal control
                                  v
                         model-server supervisor
                                  │
                         vLLM / Torch profiler
                                  │
                         persistent artifact PVC
```

| Responsibility | Owner |
| --- | --- |
| Requests, concurrency, rate and conversations | Existing benchmark executor |
| Create, finish, cancel and observe a capture request | CLI profiling orchestration adapter |
| Resolve participants, reconcile the capture and publish status | ProfileRun reconciler in the existing control plane |
| Exclusive engine capture, native utility calls and escalation | Existing model-server supervisor and its thin engine adapter |
| Artifact storage and retention | Platform-provisioned persistent storage |

There is no new benchmark Job, controller Deployment, public profiling Gateway route, tunnel, `kubectl exec/cp` result path, or general profiler plugin registry.

### Why a separate resource

A capture has an identity, desired action, deadlines, independently recoverable state and retained results. It outlives a CLI connection and must be reconciled after a controller restart. These are different from the long-lived serving intent in ModelService.

A `ProfileRun` therefore earns a separate namespaced resource and reconciler. Putting transient capture commands in `ModelService.spec` would mix lifecycles and could invalidate serving generations. Encoding the same lifecycle in a Job plus command/status ConfigMaps would still create a controller-like protocol without a clear API.

Illustrative request:

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: ProfileRun
metadata:
  name: qwen-diagnostic
  namespace: foretoken-demo
spec:
  modelServiceRef:
    name: quickstart-qwen3-0.6b
  duration: 15s
  action: Capture
```

The Kubernetes UID is the capture identity; a reused resource name is not the same run. `action` progresses from `Capture` to `Finish` or `Cancel`. The target and duration are fixed once execution starts. This is an execution request, not a content-hash contract.

Status exposes phase, observed generation, actual timestamps, participant counts, a concise failure reason and an artifact reference. It does not expose Pod addresses or a rank inventory. The controller retains stable participant identities in a ConfigMap owned by the ProfileRun. Before sending any start operation, it persists the run's cancellation finalizer and the plan containing the ModelService UID, committed serving generation, and selected Group/runtime identities. A retry reads that same plan rather than resolving replacement participants. Only acknowledged API writes permit start; no engine can be started before its recovery record exists. It resolves participants from the selected ModelService's committed serving generation, not by duplicating routing policy. No Pod or internal endpoint becomes a benchmark target or user parameter.

## Start, stop and recovery

The control plane handles `Pending → Starting → Capturing → Stopping`, followed by `Succeeded`, `Failed` or `Cancelled`. Status follows observed runtime results; merely sending an HTTP request does not advance a capture to success.

The engine adapter must distinguish successful start, confirmed stop/flush, and native failure. The pinned vLLM wrapper currently logs some native errors without propagating them. Supporting that runtime requires a narrow, reusable upstream error-propagation extension; an ACK plus a file-existence check is not a replacement. Until the adapter can meet this contract, it reports profiling as unsupported rather than advertising reliable capture.

The runtime admits at most one active capture per engine instance. Repeated operations with the same run UID return that run's current state without restarting it or extending its deadline. A different run receives `Busy`; it is not queued or silently substituted.

State transitions take a short lock. Native engine calls run outside that lock in an operation task owned by the supervisor, not by the HTTP request. Cancellation or client disconnection cannot discard that task. The supervisor continues processing deadlines and termination while a utility is in flight.

Capture duration, start-operation timeout and stop/flush timeout are different budgets, with defaults owned once by the profiling runtime configuration. They are not calculated from request count multiplied by the benchmark HTTP timeout.

A stop timeout means the engine's state is unknown. For a profiling-enabled diagnostic instance, the supervisor closes admission and invokes its existing process-group termination path without first waiting on the stuck profile utility or its lock. It confirms process exit before treating partial output as no longer writable. This can interrupt requests on that instance; profiling is not an always-on, non-disruptive production feature. If termination cannot be observed, status remains explicit about that failure rather than claiming a stopped engine or a mathematically unconditional shutdown deadline.

Controller restart is handled by re-reading the execution plan and querying the same runtime identities. Runtime replacement or a serving-topology change is reported as incomplete coverage; it does not redirect control to newly created instances. The controller never changes autoscaling settings to make a profile succeed. Explicit deletion of a live ProfileRun requests cancellation and prevents new capture starts. Its finalizer retains the execution plan until stop or process exit is observed; an unconfirmed stop remains diagnosable rather than losing the plan to garbage collection. Cancellation takes precedence over delayed retries for the same run UID. Removing the finalizer and plan never removes the artifact PVC or sealed manifests.

## Persist before publishing completion

The model-server writes directly to a dedicated, namespace-local artifact PVC supplied by the platform. This is not the model download cache, an `emptyDir`, or the benchmark's local output directory. Storage must be writable by every participating runtime and survive model Pod replacement. The PVC has no owner reference to the model service or ProfileRun.

Use one exclusive staging directory per runtime instance and one retained directory per capture:

```text
<artifact-volume>/.staging/<runtime-instance>/
<artifact-volume>/runs/<run-uid>/<runtime-instance>/
```

The engine uses the fixed staging path. Before capture, the supervisor establishes exclusive ownership of an empty staging directory; it never erases unhandled partial output. After stop, all expected workers must have returned and the adapter must verify the required trace artifacts and their format. It then renames the entire directory on the same filesystem into the run directory and publishes a small manifest. A later capture recreates an empty staging directory.

This preserves nested outputs and fixed-name reports without file-set differences, filename-prefix guesses, hashes or per-file copying. Renaming is atomic for one participant, not for the entire distributed capture. The controller publishes success only after every expected participant has sealed an acceptable result. Repeated finish requests return the existing manifest; they do not overwrite a retained directory. Startup CUDA-graph profiling must not write into the active capture staging area.

The artifact reference identifies the PVC, run directory and manifest. The local `--output-dir` may contain the run summary and reference; it is not advertised as containing downloaded Torch traces. Users access the trace through the platform's existing storage access. Automatic workstation download and a new artifact-serving API are outside the first version.

Namespace deletion can still delete namespace-local storage. This is why profiling initially requires an existing deployment and bypasses benchmark auto-deployment cleanup. Explicit namespace/PVC deletion remains a storage-owner operation; persistence beyond that boundary is not promised.

## Failure semantics

| Event | Required outcome |
| --- | --- |
| CLI exits or loses connectivity | Runtime duration still stops capture; the run and stored output remain discoverable through Kubernetes |
| One participant fails to start | Stop participants that did start; retain diagnostics; fail the run |
| Native start/stop utility never returns | Supervisor processes its independent deadline and escalates without the session lock |
| Engine returns ACK but writes no valid trace | `Failed/EmptyCapture` or the concrete storage error, not success |
| Pod or serving generation changes | Report incomplete coverage and preserve available partial output |
| Storage cannot flush or seal output | Fail publication; do not claim results were saved or delete recoverable staging data |
| User interrupts the workload | Existing executor stops request scheduling; orchestration requests cancellation; runtime deadlines provide the fallback |

Access to ProfileRun creation/cancellation uses Kubernetes RBAC and is distinct from permission to send inference requests. Internal capture control is not exposed by the public Frontend/Gateway. NetworkPolicy follows the existing platform trust boundary and is not described as per-user authentication within a shared namespace.

## Implementation boundary and acceptance

The independent profiling PR should begin from this lifecycle, not preserve the prototype's local port-forward class, Pod-copy recovery, old request scheduler or file-set bookkeeping. It must align its benchmark adapter with the standard executor refactor before restoring a user-facing profile flag. The common API carries capture intent and status; vLLM utility calls and Torch-specific validation stay in the engine adapter.

Validation must exercise a real diagnostic deployment: a complete capture whose trace opens correctly, a second capture without mixing files, interruption with continued recoverability, controller restart, a stuck utility, and failed storage publication. Inference must remain usable after normal completion; the failure case must demonstrate the documented supervisor behavior. Use the existing validation workflows and direct experiments rather than adding a permanent CI matrix or merge gate.

The earlier A100 prototype demonstrated only successful single-node capture and manual recovery after a short control timeout. It does not validate this proposed architecture. This document changes no CRD or production code.

## Upstream basis

- [vLLM profiling](https://docs.vllm.ai/en/latest/contributing/profiling.html): native profiler configuration and diagnostic purpose.
- [Pinned vLLM worker](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/vllm/v1/worker/gpu_worker.py): a Torch wrapper can retain its initial worker name across captures.
- [Pinned vLLM profiler wrapper](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/vllm/profiler/wrapper.py): start/stop errors can be logged without reaching the utility caller; ACK alone is insufficient.
- [Pinned EngineCore client](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/rust/src/engine-core-client/src/client.rs): profile utilities wait for engine replies without an operation deadline.
- [PyTorch profiler](https://docs.pytorch.org/docs/2.14/profiler.html): reuse capture and export; schedules advance with profiler steps, not wall-clock time or benchmark conversations.
- [Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/): terminating a load-generator Job would not stop a profiler in another workload, and a Job is not needed to preserve the current benchmark boundary.
- [Dynamo Profiler](https://github.com/ai-dynamo/dynamo/blob/main/docs/fern/pages/developer-guide/knowledge-base/modular-components/profiler/overview.md): deployment characterization differs from this bounded runtime trace capture.

<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Admission lifecycle

English | [简体中文](admission-rules_zh.md)

Admission chooses which prepared request can be submitted; Router chooses its backend. For configuration examples, see [Frontend admission](../../data-plane/frontend/README.md#configure-admission-rules).

A generation request follows one lifecycle:

```text
Reserve waiting capacity → wait for model readiness and prepare input
→ select a caller's turn and reserve concurrency → route and submit
→ backend acceptance → engine execution → confirmed completion
```

Waiting and caller concurrency are shared across replicas of one FrontendService, separately for each public model. Local queue order and caller rotation remain frontend-local. The gateway supplies trusted caller and role identity and owns authentication, authorization, and usage quotas.

## Waiting and dispatch

| Phase | Waiting capacity | Caller concurrency |
| --- | --- | --- |
| Readiness waiting, input preparation, or waiting for a turn | Held | Not held |
| Submitting, acceptance unresolved | Held | Held |
| Backend accepted | Released | Held |
| Between execution stages | Released | Held |
| Completed or confirmed terminated | Released | Released |

Reserve the complete batch before preparing or submitting children. Every prompt/candidate combination is a unit, including `best_of` candidates. Splitting transfers each existing slot once; it does not acquire capacity again. CPU-only tokenization, counting, and detokenization use waiting ownership but never reserve backend concurrency.

After input preparation, dispatch selects the highest eligible priority, rotates among callers at that priority, and selects the caller's oldest request. Preparing, concurrency-full, and temporarily busy callers do not block other eligible callers. Role-resolved Pool restrictions constrain readiness observations and every routing stage. Rotation shares dispatch opportunities, not token throughput or GPU time. Native engine priority conversion belongs to the backend adapter, not Router scores.

The HTTP request retains its intake request and stream-idle budgets across configuration changes. The admission waiting deadline covers readiness, preparation, and initial submission, and cannot exceed the original total deadline. Non-cancelable CPU work retains its permit until the work actually finishes, even when the HTTP caller has already timed out or disconnected.

## Acceptance and cancellation

The shared ledger atomically reserves capacity and transfers each candidate to a model-server execution owner. Store unavailability prevents new bounded reservations; accepted work retains its ownership and retries ledger cleanup. Model-server acceptance is distinct from beginning GPU execution; the engine may still queue the request. An optional instance acceptance cap is separate from caller concurrency.

Only a definite nonacceptance permits an initial retry. A Router Busy result or backend `503` with `admission_busy` or `admission_unavailable` returns the unaccepted dispatch reservation while preserving waiting order and deadline. Invalid input, incompatible execution paths, and ambiguous transport failures terminate the attempt rather than replaying it. Once any batch child or execution stage is accepted, the initial request is not retried.

Before submission, cancellation removes pending work. After acceptance, cancellation requests backend termination and leaves concurrency charged until termination is confirmed. Output-stream drop, an abort acknowledgement, and locally synthesized abort output are not confirmation. The execution owner retains a completion signal independent of output delivery; if that signal is lost, managed-engine shutdown must be confirmed before releasing uncertain execution. See the [vLLM completion contract](../../data-plane/patches/vllm/README.md#engine-completion-contract).

## Prefill/decode handoff

Prefill/decode (P/D) and encoder/prefill/decode (E/P/D) share one outer candidate reservation. Encoder and Prefill are intermediate stages; their confirmed completion moves the slot into handoff without decrementing caller concurrency. Their terminal output waits for that ledger transition so the next stage can claim the same slot. Decode is the final stage and releases the slot after confirmed completion.

Instance permits and routing-load observations end with their own stage; they are not held merely to wait for the next stage. Connector-owned KV and media-transfer resources retain their separate cleanup lifecycle. Engine completion confirms that a request has left engine scheduling, not GPU synchronization or connector resource release.

## Configuration and observations

Validate a complete candidate before publication. Invalid candidates preserve the active configuration. Controller-observed Pod membership advances independently so confirmed instance termination can release capacity even when a candidate is rejected. Publication advances shared settings independently of new traffic, and dispatch uses the current shared limits. Bounded admission acknowledgement waits for ledger application; unrestricted settings can activate locally. Queues, outstanding reservations, and original waiting deadlines survive ordinary limit changes; lowering a limit does not cancel accepted work. A waiting batch larger than its new caller concurrency limit is rejected as an oversized batch.

Controllers publish instance acceptance limits independently of the immutable engine configuration. Each running model-server applies the limit without replacing its execution permits or changing its accepting/draining state. A lower cap prevents new acceptance while unfinished work is at or above that cap. Configuration acknowledgement waits until the requested instances report the applied settings; the engine's own scheduling limits remain unchanged.

Pending requests resolve current roles and Pools when receiving a dispatch grant. Already-granted batches retain their resolved decision. Enabling bounded rules rejects untracked requests that have not yet dispatched; it does not retrospectively account for unrestricted execution. Model removal rejects new and waiting work while accepted execution drains.

Record one admission result at first backend acceptance, CPU-only preparation completion, or terminal failure. Busy retries are not failures. A request's observation can finish before non-cancelable preparation or storage cleanup releases capacity. Shared occupancy gauges report ledger state and must be deduplicated across frontend replicas; Prometheus observations never grant or release capacity. Operational metrics are described in [Observability](../../observability/README.md).

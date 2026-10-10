<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Admission lifecycle

English | [简体中文](admission-rules_zh.md)

Admission decides which prepared request can run; Router selects its backend. For user configuration, see [Frontend admission](../../data-plane/frontend/README.md#configure-admission-rules).

```text
Reserve waiting capacity → wait for readiness and prepare input
→ reserve caller concurrency → route and submit
→ backend acceptance → execution → confirmed completion
```

Model waiting capacity and per-caller limits are shared across replicas of one FrontendService, separately for each public model. Dispatch order remains local to each frontend: highest eligible priority first, round-robin between callers at the same priority, and FIFO within each caller. A blocked caller does not prevent other eligible callers from progressing. The gateway owns caller authentication and usage quotas.

## Capacity ownership

| Phase | Waiting capacity | Caller concurrency |
| --- | --- | --- |
| Readiness waiting, input preparation, or waiting for dispatch | Held | Not held |
| Submission awaiting acceptance | Held | Held |
| Backend accepted, including engine queuing | Released | Held |
| Between execution stages | Released | Held |
| Completed or confirmed terminated | Released | Released |

Reserve a batch's full capacity before submitting any child. Each prompt/candidate combination, including `best_of`, counts once; splitting transfers existing capacity rather than acquiring it again. Tokenization, counting, and detokenization use waiting capacity only. Non-cancelable preparation retains that capacity until the work ends, even if its HTTP request has already ended. One request deadline covers waiting, preparation, retries, and every execution stage.

After acceptance, the backend owns execution and completion. Cancellation requests termination but does not release concurrency until execution ends. Closing an HTTP stream is not completion; uncertain execution must be stopped before its capacity is released. Backend integration details are in the [vLLM completion contract](../../data-plane/patches/vllm/README.md#engine-completion-contract).

Only definite nonacceptance permits retry: Router Busy, or backend `503` with `admission_busy` or `admission_unavailable`. Return the dispatch reservation while keeping the original waiting position and deadline. Once any batch child or stage is accepted, do not replay the initial request. Store unavailability prevents new bounded reservations; accepted work continues and completes its accounting when the store recovers.

## Disaggregated execution

Prefill/Decode (P/D) and Encoder/Prefill/Decode (E/P/D) share one caller-concurrency reservation per generated sequence. Intermediate stages release their own instance capacity when they finish, while caller concurrency remains held through the next stage. Decode releases it after confirmed completion. Complete the ownership transfer before returning intermediate completion to the next stage. Connector-owned KV and media resources have their own cleanup lifecycle.

## Configuration updates

Validate settings before activation; invalid settings leave the working configuration unchanged. Authoritative Pod membership still updates independently so terminated owners can be reclaimed. Confirm bounded settings only after their shared limits take effect.

Limit updates preserve outstanding work, queue order, and original request deadlines. Lower limits do not cancel accepted execution; a waiting batch larger than its new caller-concurrency limit is rejected. Pending requests use current role and Pool rules at dispatch, while already-granted batches retain their decision.

Enabling limits rejects previously untracked requests that have not dispatched, without retroactively counting existing unrestricted execution. Removing a model rejects new and waiting requests while accepted work drains.

Instance acceptance limits are separate from the engine's scheduling limits. A live update changes only new acceptance: existing work and the instance's accepting or draining state remain unchanged. A lower limit takes effect as occupancy falls, and configuration status is confirmed after the requested instances report the new value.

## Observations

Record one admission result at first backend acceptance, CPU-only preparation completion, or terminal failure. Busy retries are not failures. Shared occupancy must be deduplicated across frontend replicas; metrics observe capacity but never grant or release it. See [Observability](../../observability/README.md).

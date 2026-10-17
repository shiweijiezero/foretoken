<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Request-level Draft/Target routing

English | [简体中文](frontend-workflow_zh.md)

Frontend normalizes input, sampling and stop controls and obtains the existing
request admission reservation. Router selects one Target and one compatible Draft
through its ordinary Filter → Scorer → Picker pipeline. The binding lasts for the
request. Target owns its rounds; each engine's native Scheduler owns token budgets,
KV allocation, preemption and physical batching. There is no separate shared DT
scheduler process or deployment.

## Run the token-input diagnostics

Start roles using the [README](../README.md). Build from the repository root:

```bash
make vllm-source
cd data-plane
cargo build --locked -p foretoken-server --examples
```

Set `DRAFT_URL` and `TARGET_URL` to running roles. A minimal `request.json` is:

```json
{"request_id":"example","prompt_token_ids":[1,2,3],"sampling_params":{"temperature":0,"max_tokens":24}}
```

```bash
./target/debug/examples/draft_target "$DRAFT_URL" "$TARGET_URL" < request.json
```

Use real IDs from the shared tokenizer. This low-level input does not infer EOS:
`_eos_token_id`, `stop_token_ids` and `_all_stop_token_ids` belong to model-aware
normalization. The caller owns its deadline. The selected endpoints must be reachable
from the frontend and from the Target role, which calls the Draft directly. Use
per-instance endpoints: a load-balanced address that switches instances cannot
preserve these engine sessions.

For concurrent requests, put the worker inventory in `batch.json`:

```json
{
  "model": "example-model",
  "workers": [
    {"endpoint":"http://draft-host:19100","scope":"dt"},
    {"endpoint":"http://target-host:19100","scope":"dt"}
  ],
  "requests": [
    {"request_id":"a","prompt_token_ids":[1,2,3],"sampling_params":{"temperature":0,"max_tokens":8}},
    {"request_id":"b","prompt_token_ids":[1,2,4],"sampling_params":{"temperature":0,"max_tokens":24}}
  ]
}
```

```bash
./target/debug/examples/draft_target_batch < batch.json
```

Add Draft and Target entries for a fixed N:M deployment. The Router chooses one
pair per request; compatible Drafts can serve requests bound to different Targets.
Stdout contains ordinary `TokenOutput` NDJSON, while stderr reports selected
endpoints. Ctrl-C closes the streams. EOF without terminal output is a failure.

## Execution ownership

```text
Frontend → Router: choose [Draft] + Target once, retain routing reservations
         → Target RoleService.generate → TargetCoordinator (one per request)
             ├─ open the selected Draft control session at the prompt frontier
             ├─ native Target prefill → confirmed delta + ticket
             ├─ commit exact confirmed delta to Draft
             ├─ Draft propose → native Draft generation → candidate IDs / handle
             └─ local verify: prepare one proposal → native ticket admission
Frontend ← confirmed output ← native Target sampling and stop processing
```

`TargetCoordinator` retains the cumulative confirmed output frontier, round and
fixed peer binding and propagates confirmed deltas. Native Target state owns its
complete token prefix; `DraftSession` retains the synchronized prefix used for
ordinary per-round Draft requests. `RoleService` owns native sessions, current tickets,
started operations and cleanup. A native ticket is an internal request ID plus
confirmed output count; admission consumes it once. Candidates never advance the
confirmed prefix. Target selects candidates from its configured default (3), capped
by remaining output. Engine capacity is configured separately; see the
[candidate contract](role-protocol.md#candidate-policy-and-capacity). No cross-request
stage policy, waiting window or logical batch is introduced. Native scheduling
groups independently ready requests.

A slow Draft or transfer suspends only its request's coroutine. Others can reach
native admission and share a physical batch. Each native batch observation records
its ID, wall-clock interval, scheduled tokens and members' engine request IDs,
client request IDs, confirmed frontiers and `{request_id, work_id, frontier, round,
phase}` identities. HTTP calls are not GPU batches; schedule-to-result durations
are wall time, not GPU-event measurements.

The Foretoken router owns request placement and routing load. DT plugin adapts
native Scheduler, Worker and ModelRunner layers, preserving native KV, batching,
forward and sampling. Maintained vLLM patches supply required low-level hooks and
platform fixes. Prefill uses the normal Target path and appears in native workload.
The plugin package entry point must be loaded in spawned engine processes too;
its hooks are restricted to the pinned versions in the
[MRV2 integration contract](mrv2-integration.md).

## Strategy development boundaries

Request placement belongs to the existing Router Filter → Scorer → Picker pipeline.
`RouteCandidate` supplies compatibility, current telemetry and routing reservations;
`RouteSession` retains and releases the selected pair with the request lifecycle.

`TargetCoordinator.select_candidate_count()` is the round-budget extension point
called by `advance()`. Its default reads the launcher-owned configured K (3 when
omitted) and caps it by remaining output, the bound Draft method limit and native
Target capacity. The coordinator retains frontier, ticket, submission and cleanup
ownership; the selection method neither submits work nor mutates those facts.
The configured default and startup capacity remain separate values. There is no
policy registry or dynamic-K algorithm.

Local batching belongs to each engine's Scheduler. `ObservedScheduler.schedule()`
observes native decisions; `ExternalScheduler.schedule()` excludes requests still
waiting for remote inputs, then calls native scheduling. Algorithms changing
members, priority or execution order belong at that Scheduler boundary and must
preserve native token/KV accounting and preemption. Worker/Runner and Connector
continue to prepare, execute and release the selected work.

Round feedback includes actual requested/proposed/verified/accepted/committed token
counts, nullable engine ticket `admitted`, proposal transfer and `context_transfer`
for Target-to-Draft features. Request, round, work, frontier and worker endpoints
link to native physical batches. Frontend records these as traces and bounded
metrics, including both transfer directions; host wall durations are not GPU time.
Role logs link proposal/verification work identities to artifact handles and feature
sources. Completed reads and producer ACKs are logged even when cancellation
prevents a final round observation.

## Managed profiling

DT model-server uses the same profiling supervisor, ProfileRun API, deadlines and
artifact publication as ordinary services. Private role HTTP calls only bridge
capture commands to native engine utilities. On MetaX, the existing mcTracer
Worker mixin wraps the platform-preserving DT Worker; no model execution body is
copied. Select `ModelService.spec.profiling.engine: mctracer` and use the existing
[`foretoken perf/profile` workflow](../../../benchmarks/docs/profile/README.md).
An uncertain capture operation remains supervised until engine process exit.
Run performance measurements separately without capture.

## Cancellation, drain and recovery boundary

Frontend stream closure explicitly closes the Target session. Target closes its
Draft control session, aborts native work and joins started operations. Transfer
owners finish reads and ACKs even if their HTTP caller disconnects. Buffer release
waits for compute fencing and consumer ACKs. Failed cleanup remains failed; session
state cannot claim `closed` while a known cleanup failure remains.

Drain rejects new bindings while existing rounds finish. Existing frontend streams
retain their chosen clients when discovery changes. Multiple frontend replicas
independently route new requests; each accepted request has one Target round owner.
A peer or process failure terminates that request without transparent replay.

The production path currently uses a fixed single Draft and Target and one candidate
chain. Token-only execution is greedy; tensor adapters also support native random sampling. The endpoint list preserves the future
multi-Draft boundary; multiple entries are rejected today. Existing RDMA/neural
operations and manual checkpoint/resume remain adapter capabilities, with their
model, sampling and layout restrictions. Automatic migration and a new placement
policy are not implemented. See the [role protocol](role-protocol.md) and
[connector contract](connector-contract.md).

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Frontend Draft/Target execution

The Rust frontend separates request state, round placement and round execution. `foretoken-llm-facade` owns
HTTP role sessions and response decoding; `foretoken-server::draft_target`
executes proposals, Target verification and confirmed-prefix updates for one round.
The public runtime owns a per-model ready queue; custom callers can also regroup
requests before selecting the next round. Each model's
scheduler retains local batching and KV ownership.

The executable example runs that frontend library against two existing role
services. It is a token-input integration entry point, not an OpenAI server or
an alternative deployment controller. Build from the repository root:

```bash
make vllm-source
cd data-plane
cargo build --locked -p foretoken-server --example draft_target
```

Set `DRAFT_URL` and `TARGET_URL` to the selected services. Supply a JSON file with
the frontend's normalized `GenerateInput` fields. For example, using token IDs
from the Target tokenizer:

```json
{"request_id":"example","prompt_token_ids":[1,2,3],"sampling_params":{"temperature":0,"max_tokens":24}}
```

Use actual prompt tokens. `_eos_token_id`, `stop_token_ids` and
`_all_stop_token_ids` must come from the frontend's model-aware normalization;
the minimal example above has no automatic EOS termination. Run:

```bash
./target/debug/examples/draft_target "$DRAFT_URL" "$TARGET_URL" < request.json
```

The command prints standard `TokenOutput` NDJSON, including the original request
ID, one-time prompt metadata, finish/stop reasons and cached-token count. It checks role health, admission state
and candidate format before opening a request, and uses the smaller advertised
candidate budget. Ctrl-C drops the output stream and both role sessions.
Callers remain responsible for matching tokenizer semantics and enforcing their
request deadline. There is no retry or transparent replay after a role fails.

## Run per-round batches through Router

Build the batch entry point with `cargo build --locked -p foretoken-server
--example draft_target_batch` from `data-plane`. Supply one JSON object:

```json
{
  "model": "example-model",
  "workers": [
    {"endpoint": "http://draft-host:19100", "scope": "dt"},
    {"endpoint": "http://target-host:19100", "scope": "dt"}
  ],
  "requests": [
    {"request_id": "a", "prompt_token_ids": [1,2,3], "sampling_params": {"temperature": 0, "max_tokens": 8}},
    {"request_id": "b", "prompt_token_ids": [1,2,4], "sampling_params": {"temperature": 0, "max_tokens": 24}}
  ]
}
```

Replace endpoints and prompt tokens, then run:

```bash
./target/debug/examples/draft_target_batch < batch.json
```

The example reads each worker's live status and builds a Router inventory. Workers
in one scope must use the same tokenizer identity; callers must also ensure their
token-ID semantics match. More worker entries provide placement choices. Standard
`TokenOutput` events retain each request ID; selected worker IDs go to stderr.
Ctrl-C closes the whole batch.

The caller uses three interfaces:

| Interface | Input and result |
| --- | --- |
| `DraftTargetRequestState::new` | Normalized `GenerateRequest` and a `CancellationToken`; owns the request's confirmed prefix and remote sessions across batches. |
| `select_draft_target_round` | Router, resolver, model and caller-owned `&mut Vec<DraftTargetRequestState>`; returns `Some(round)` and takes its members, or `None` and leaves them queued. |
| `DraftTargetRound::execute` | Returns one result per member: optional confirmed output and an optional `continuation`. A refused session returns only a continuation for new placement. Finished or cancelled requests have no continuation. |

For one already-formed batch, the call sequence is:

```rust
let Some(round) = select_draft_target_round(&router, &resolver, model, &mut ready).await? else {
    // Keep ready; retry placement after worker observations or capacity change.
    return Ok(());
};
let mut results = round.execute();
while let Some(result) = results.next().await {
    let result = result?;
    // Dispatch result.output to its request's consumer.
    // Return result.continuation to the scheduler's ready queue.
}
```

A scheduler must poll concurrent round streams and dispatch ready continuations
without waiting for every original member. Some initial prefills may still be
queued behind vLLM's `max_num_seqs`; waiting for all of them before advancing
ready requests can deadlock. The runnable example uses `SelectAll`, drains the
currently ready results, groups them by the executor's `required_target`, and
selects a new round.
Requests from different earlier batches can join the same later round.

Router reuses Filter–Scorer–Picker with the first member's placement preference,
checking compatibility for every member. It sees current confirmed prefixes and
remaining output budgets. Reservations cover the current round and are released
before each member returns; they do not measure idle sessions or KV occupancy.

`DraftTargetRequestState::target_location()` reports the current session/KV location.
`DraftTargetRound::required_target()` derives the current execution constraint;
Router selects this round's workers within that constraint. Selection does not
update location: a request records its location when the Target session opens.

Ordinary inference rounds require the current Target. Explicit `migrate_target`
changes that location before the next round. Requests at different Targets run in separate rounds;
fresh requests can join either group. Existing sessions can finish on a draining
Target, while fresh bindings still require admission. The location is request
state; the requirement belongs to the executor's supported operations.

Draft is selected anew each round: the same worker receives the confirmed delta;
a different worker receives the full confirmed prefix through a new binding.
Its ordinary vLLM requests use that worker's local prefix cache when available.
If a role explicitly refuses a new session during drain, the executor returns
the unchanged request state without output. The scheduler must enqueue that
continuation even though it has no tokens to deliver. `select_draft_target_round`
passes Target affinity and the members' known closed workers as `DraftTargetPlacement`
constraints; Router excludes those workers before compatibility checks and scoring.
Drain is one-way for that worker identity. If no compatible worker is selectable,
selection returns `None`; the public scheduler waits for an observation refresh
or released reservations. Network or inference failures are not replayed. Standalone examples report unavailable when their fixed worker inventory has no
selectable pair; they do not subscribe to production runtime updates.

A fresh request first receives Target prefill output. A ready request generates
candidates on the selected Draft, then joins one `/batch/verify` submission.
Target prepares tensors and admits candidates in one EngineCore utility call;
vLLM still chooses physical GPU batches using its own token and KV capacity.

Dropping a round closes members still in that round; returned continuations belong
to the caller. Dropping a continuation closes its sessions. Cancellation interrupts
in-flight work; the scheduler must discard idle cancelled continuations. A worker
or protocol error terminates the affected round. The example has no timed queue
or SLO policy. Public handlers use `DraftTargetScheduler`, which accepts new
requests throughout execution and dispatches output separately to each client.

## Move a request between Targets

Start two compatible NVIDIA Target roles with RDMA and `--enable-kv-migration`.
Both must use the same model, revision, tokenizer and full-attention KV layout.
This path supports greedy sampling and one GPU per role. It does not support
MetaX, hybrid attention or random-sampling state migration.

Between rounds, call `state.migrate_target(resolver, destination).await?` with a
Target `RouteDecision`. The result has the same output/continuation shape as an
inference round. The caller chooses the destination; the public scheduler does
not invoke this operation automatically.

The batch example accepts an optional top-level field:

```json
"migrations": [{"request_id": "a", "after_round": 1, "target_worker": 2}]
```

`target_worker` is a zero-based index in `workers`; add the second Target there.
Round zero is prefill, and migration also consumes a round number. Router chooses
the initial Target independently: inspect the selected-worker trace and choose a
different destination. A plan naming the current Target fails, as does a plan
whose round is never reached before the request finishes.

The executor checks compatibility, asks the source to copy its resident KV, and
opens a destination session with the same confirmed prefix and remaining output
budget. The destination receives the checkpoint through Mooncake, then its native
scheduler allocates pages and loads KV before the first forward. Locally cached
prefix pages are reused. The destination computes any remaining uncomputed suffix;
it does not replay the transferred prefix. After the first destination output,
the executor updates the location and closes the old Target and Draft bindings.

The source session remains owned until the destination proves it can continue.
An error or cancellation closes both sessions and ends the request; there is no
transparent retry or rollback. Cancelling before claim revokes the checkpoint;
after claim, the source copy remains until the receiver finishes its read and ACKs.
Native vLLM still owns allocation, prefix caching and preemption. If the source
has already lost resident KV to preemption, export fails instead of presenting a
prefix replay as migration.

## Normalized request semantics

`generate_draft_target_tokens` accepts the existing `GenerateRequest` and returns
`TokenStream`, so callers retain the current frontend text and chat output
processors. Explicit EOS/stop tokens, minimum length and maximum length are
forwarded. Python automatic EOS handling is disabled in this path: the frontend
already resolved `ignore_eos`, and only its normalized stopping tokens apply.
Stop strings remain with the frontend's decoder and are not sent through this
normalized adapter.

Sampling forwards `temperature`, `top_p`, `top_k` and Target `seed`. Random
sampling requires both roles to advertise `token_ids_log_probs`; `min_p` is not
supported by the native speculative sampler. Non-default penalties, logprobs, structured output,
multimodal features, LoRA, KV/EC transfer arguments, cache salt, priority, tracing
headers and nonzero DP ranks are rejected before role admission. These are current
implementation limits, not ignored options. The lower-level role API still
supports direct token requests and optional Python-side stop strings.

The allowlist applies after model generation defaults are resolved. For example,
a model default of `repetition_penalty: 1.1` causes an HTTP 400 even when the client
omits that field. Explicitly requesting `repetition_penalty: 1.0` disables the
penalty and permits this supported path; it changes the requested sampling policy.

## Ownership during a round

```text
Frontend                                Target               Draft
   |-- generate(prompt) ------------------>|                    |
   |<-- confirmed token + generation ------|                    |
   |-- bind(confirmed prefix, generation) --------------------->|
   |-- propose(generation, budget) ---------------------------->|
   |<-- candidate IDs + artifact handle ------------------------|
   |-- verify(ticket, IDs, artifact) ----->|                    |
   |                                      |-- claim artifact ->|
   |                                      |<-- GPU descriptor -|
   |                                      |<== Mooncake log(q) =|
   |                                      |-- read ACK ------->|
   |<-- confirmed delta + next generation |                    |
   |-- commit(delta, next generation) ------------------------->|
```

The frontend relays artifact handles, never GPU addresses or probability arrays.
Target claims a handle directly from Draft before reading. Draft cancellation
reclaims an unclaimed buffer; a claimed buffer waits for Target's ACK. Target
admits verification after its Worker completes the read. Its claim/read/ACK task
retains transport ownership even if the frontend disconnects.

Request state owns both HTTP response bodies through every await. During execution,
dropping the round stream cancels its still-owned sessions even while a proposal
is outstanding. Nonterminal output transfers state to the caller; terminal output
releases sessions before being yielded. An EOF without a terminal commit is a protocol failure.
The workflow never emits Draft guesses or rebuilds token state from display text.

## Discovery and public API dispatch

The normal frontend can consume a `dt_components` array in its
serving snapshot. `models` retains the public Target identity, tokenizer and
Pool admission set. Each DT component declares `service_uid`, `pool_uid`,
`pool_name`, `route_target_id`, `pipeline_scope_id`, `role` (`draft` or `target`),
`model` (the public model), `engine_model`, nullable `engine_revision`, and
`endpoint`. The loaded Draft weights may differ from the public Target model.
A scope needs both roles, with the same service, public model and admission set;
DT and P/D cannot be combined for one model in this implementation.

Registry health probes use `/status?observations=false`, which does not wait on
GPU execution. They check the actual role, weight identity, tokenizer identity,
candidate format and admission state. Both engines must report the service's
configured tokenizer and revision. For local snapshots the engine revision is
null; local paths must resolve to the intended artifacts on every host. Equal
path names alone do not establish equal file contents. This first implementation
requires a common tokenizer; different-tokenizer speculation is unsupported.

Public text/chat dispatch enters a per-model `DraftTargetScheduler`. It immediately
forms rounds from currently ready requests, grouping existing sessions by Target
location and separating greedy from random sampling. It does not wait for a fixed
batch size or for every member of an earlier batch. Pending prefills and output
deliveries are polled alongside active rounds; a slow client pauses only its own
continuation. A request's deadline or disconnect cancels its state at admission,
during execution, or between rounds. When Router has no selectable compatible
pair, members stay queued until worker observations refresh or another round
releases reservations. Waiting does not require an idle GPU: accepting roles use
vLLM's native queue when local execution slots are occupied. Cancellation removes
waiting members without waiting for a worker update. Only an explicit absence of
selectable routes is queueable; malformed routing algorithm results remain errors.

Filter–Scorer–Picker selects Target and Draft for each batch. Target selection
requires a compatible healthy Draft; random sampling excludes token-only roles.
Existing sessions keep their current Target; this public scheduler does not invoke migration.
The selected round owns both workers' per-member reservations, releasing them as
results return. No separate lifetime DT reservation is retained. DT publishes native
scheduler/KV telemetry to BackendRegistry; existing configured scorers consume
those observations and frontend reservations. There is no KV-index source for
prefix-hit prediction, SLO batching policy, or measured migration cost model.

`RuntimeBuilder` derives DT models from the validated serving snapshot and reuses
existing tokenizer, request deadline, output processors, and public API handlers.
A successfully published DT model appears in `/v1/models`. Each immutable runtime
generation owns its queues and worker inventory. When a newer snapshot replaces
it, requests queued without a Target session return to the HTTP owner and are
prepared again against the new runtime, including its tokenizer and chat renderer.
The original deadline and cancellation remain in force. Requests with live Target
sessions keep their original inventory, whose observations continue refreshing
until its last response closes.
Invalid snapshot updates leave the active generation intact. Role
readiness refresh removes draining Drafts from new selections; bound Target
sessions can finish on their draining owner. Worker/protocol errors terminate the
outstanding members of the affected round; there is no retry or replay.
Readiness refresh is asynchronous. An explicit Draft or Target session refusal returns
only the affected member for new placement; siblings with proposals continue
through verification. Transport errors and failures after binding remain fatal.

**Deployment boundary:** `ModelService.spec.speculation.draftPool` references
one Aggregate Pool with explicit Draft weights in `modelPools[].model`. The
remaining Aggregate Pools use the service model and verify candidates. The
controller derives `speculationRole` on Pool templates and Groups independently
of their deployment `role`; this selects the DT launch plan and snapshot projection.
Pools without speculation continue through ordinary Aggregate serving. P/D
composition is currently rejected. The controller publishes `dt_components` only
for ready committed replicas, preserving
service model/tokenizer identity separately from each role's engine model. The
model-server supervises the DT application using the existing launch argument
renderer and process owner. Role `/healthz`, `/readyz`, telemetry and admission
closure endpoints integrate with the existing bounded Group drain. The supervisor
waits for both active sessions and retained transport artifacts. Existing platform
RDMA allocation enables the Worker connector; Pod IP supplies its handshake address.

The role reports owned session counts for drain separately from native scheduler
workload and KV allocation.
Metric-driven autoscaling remains unvalidated. This path requires the plugin and
the exact native vLLM version in the [integration contract](mrv2-integration.md).

## Read round and placement observations

The batch example prints each completed round as `round: <JSON>` on stderr;
stdout retains the token-output stream. Each record includes worker endpoints,
context and batch sizes, stage durations, proposal/verification/acceptance counts,
transfer bytes and Target KV allocation. `workloads()` returns the two selected
workers' timestamped native queue/KV snapshots. The production registry makes
these worker observations available to Router scorers through the existing direct
telemetry path.

Between rounds, a scheduler can query a retained request with
`state.target_kv().await?` and read `state.target_location()` and
`state.context_tokens()`. Native preemption can change KV residency without
changing the session owner, so a previous round's KV observation is not a lease.
The example still dispatches ready requests immediately; it does not implement a
cost model or migration policy.

See [observation definitions](role-protocol.md#observations) before comparing
stage times or calculating acceptance. Measurements add EngineCore observation
RPCs; ordinary runtime overhead is included in frontend wall durations. These
observations do not measure GPU kernel time. The DT path does not yet integrate
the existing profiling workflow.

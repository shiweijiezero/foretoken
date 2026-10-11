<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Draft/Target role protocol

This internal protocol connects a frontend coordinator to independently deployed
models. Role endpoints execute model work; the frontend owns placement, stage
ordering, stream lifetime and cancellation. Local vLLM schedulers form batches
and own KV allocation. A version is a Target engine generation ticket, not a KV
length or a transport-buffer identity.

## One request

1. Open Target `POST /generate` with `token_ids` and `max_tokens`, optionally
   `stop` strings, `stop_token_ids`, `min_tokens`, `ignore_eos` and `sampling`.
   `sampling` contains `temperature`, `top_p`, `top_k` and optional Target `seed`.
   Read its NDJSON response. The first `opened` event supplies a
   `session_id`. Each `committed` event carries a token delta, text delta,
   `finished`, `finish_reason`, `stop_reason`, `cached_token_count` and the next
   `version` (null on termination). A draining Target instead returns
   `{"event":"admission_closed"}` before creating any session or running prefill.
   The caller may then select another Target.
2. After the first nonterminal commit, open Draft `POST /sessions` with the
   original prompt plus confirmed tokens, the Target's `version`, and the same
   `sampling` settings. Draft uses independent random draws, ignoring the Target
   seed. Read the
   `opened` event and **keep this response stream open** throughout the session.
   A draining Draft instead returns `{"event":"admission_closed"}` and closes
   the NDJSON body. This guarantees no session or proposal was created. Admission
   is decided when the body starts, including if drain happened after HTTP headers.
   The frontend may keep the Target session and select another Draft. Update the
   frontend and roles together to enable this recovery; a missing/malformed response
   or HTTP error does not provide the same guarantee and is not retried.
3. Call Draft `POST /sessions/{id}/propose` with that `version` and `max_tokens`
   within the role budget. Its result contains `version`, `token_ids`, and
   `artifact`: null without RDMA, otherwise `{artifact_id}`. This handle does not
   expose a GPU address.
4. Call Target `POST /sessions/{id}/verify` with that version, candidate IDs,
   `artifact`, and the selected Draft HTTP `source_endpoint`. In RDMA mode Target
   first calls Draft `POST /artifacts/{artifact_id}/claim` to obtain the GPU
   `PayloadRef` defined in the [Connector contract](connector-contract.md).
   A revoked or already claimed artifact returns HTTP 409; Target reports
   `accepted: false` without starting a read. Otherwise, Target
   completes the GPU read and sends `POST /artifacts/{artifact_id}/release` to
   Draft with the exact `publication_id`, then stages data and submits candidates.
   `accepted` means the engine admitted this candidate submission, not that its
   tokens were accepted by verification. An empty token list requests one ordinary
   Target step. Duplicate, stale and already-consumed tickets return false.
5. Read the next Target commit. If nonterminal, call Draft
   `POST /sessions/{id}/commit` with `base_version`, the new `version`, and the
   exact confirmed `token_ids` delta, then repeat from step 3. Draft discards
   guesses implicitly by using only the confirmed prefix in its next request.
6. On termination or failure, close both response streams. Explicit
   `DELETE /sessions/{id}` is also idempotent. Closing the Draft stream aborts
   any active proposal; closing a proposal response alone is not the session's
   cancellation boundary.

The initial Target prefill produces its first token without a Draft. The
frontend must not re-tokenize display text to reconstruct commits: stop handling
can make displayed text and internal token boundaries differ. This first adapter
supports text and linear candidates and does not negotiate tokenizer compatibility; callers
must select models with matching token-ID semantics.

## Target context for neural Drafts

For a neural Draft, `/status` advertises a non-null `context_profile` identifying
the method and Target/Draft model pair. Selected roles must agree on this profile.
`propose` additionally carries `context_source: {endpoint, session_id}` for the
current Target. Independent token-only Draft models ignore this field.

Draft calls Target `POST /sessions/{id}/context` with the same `version`. Target
validates the paused engine ticket and returns `artifact_id`, the complete confirmed
`token_ids`, and `profile`. The artifact contains a dense
`[computed_tokens, feature_layers, hidden_size]` tensor in the Target model dtype;
the first layer is the final hidden state and subsequent layers are native auxiliary
outputs. The computed prefix excludes the final anchor token. Draft checks the
profile, prefix and dimensions, then uses the existing claim/read/ACK endpoints.
The proposal owns the received features until native execution finishes or is aborted.
Context transfer completes outside the Target compute loop, allowing other requests
to execute while this request waits for Draft.

## Batch verification

`POST /batch/verify` accepts `{"members": [...]}`. Each member has `session_id`
and the same fields as the single-session verify request. Session IDs must be
unique in the batch. The response is `{"accepted": [true, false, ...]}` in input
order. Closed or stale members return false without rejecting their siblings.
Malformed input and concurrent verification of the same session reject the call.

Target claims, reads and stages each live member's tensor before making **one**
`submit_external_draft_batch` EngineCore utility call. The scheduler admits each
still-valid ticket during that call, with no scheduling step between admissions.
This does not force one GPU batch: native capacity limits still apply. The existing
single-session endpoint delegates to this same implementation with one member.

Session streams, rather than the batch command, own cancellation. Cancelling one
session invalidates its ticket while other members finish their reads and admission.
The service observes every started transfer and reclaims unadmitted destination
buffers on failure before submission or explicit rejection. If the utility response
is uncertain, staged storage stays with its session until output or abort. The
frontend keeps candidates associated with each session
and version; one member's result cannot be applied to another.

## Target KV checkpoint and resume

Targets started with `--enable-kv-migration` advertise `kv_migration: true`.
The caller must pause between rounds and select a compatible destination.

1. Source `POST /sessions/{id}/checkpoint` with `{"version": ...}` copies the
   confirmed prefix's resident KV. It returns `artifact_id`, `token_ids`,
   `computed_tokens` and a per-layer `layout` (name, shape, dtype). The source
   session owns this copy; its native pages remain managed by vLLM.
2. Destination `POST /resume` receives `generation` (the normal `/generate`
   fields, using the full confirmed prefix and remaining output budget),
   `source_endpoint`, and `checkpoint` (the previous response). It opens a new
   NDJSON session, claims the source artifact, validates the layout and byte size,
   reads the uint8 checkpoint tensor, and ACKs the completed read. Proposal
   distributions remain float32 tensors; both use the same artifact lifecycle.
3. The native destination scheduler allocates pages. The connector loads received
   KV before attention, skipping local prefix-cache hits. The first `committed`
   event proves resumed execution. The caller then closes the old source session;
   subsequent proposal/verification uses the new session's generation tickets.

The Rust executor checks model/tokenizer identities and matches the exported
prefix before calling `/resume`. Direct protocol callers own these checks and
must not verify another round on the source while exporting. Resume currently
requires greedy sampling and no Python-side stop strings; frontend stop handling
continues in its existing output processor. On failure, close both sessions.
A cancelled receiver finishes claim/read/ACK cleanup without admitting new work.
Uncertain DMA or a lost peer can still retain storage until process termination.

## Concurrency and lifecycle

Each session allows one outstanding proposal. Draft commits during an active
proposal and mismatched context versions return HTTP 409. Unknown sessions return
404; malformed messages and invalid token IDs return 422. Generation failures
after response headers terminate the stream; the frontend must cancel the paired
session rather than treating end-of-stream without a terminal event as success.

`GET /status` reports model, role, acceptance state, active-session count,
candidate format and token budget. A failed EngineCore makes status and new
session admission return 503. `POST /drain` closes new session admission but allows existing proposals, commits
and verification to finish. New session streams return `admission_closed`; engine
failures and other HTTP errors are not admission-refusal signals. Drain is
irreversible for that service process. Deployment automation must wait for zero
active sessions and zero `retained_artifacts` before terminating the role. Cancelling
sessions does not prove transfer storage is released. There is no
transparent session migration or replay after a process restart.

A waiting Target stays in vLLM's request state but is not scheduled for model
execution. Ready requests can continue to batch. The Draft implementation uses
ordinary per-round requests. Independent drafts, DFlash and DSpark can reuse native prefix
caches; EAGLE/MTP disable Draft prefix caching because their KV depends on shifted
tokens. The implementation does not promise optimal Draft KV reuse.

## Transport and extension boundary

Both roles advertise `token_ids_log_probs` when started with RDMA, or
`greedy_token_ids` without it. The selected pair must match. Random sampling
requires RDMA. Tensor data moves directly between workers; frontend relays only
candidate IDs and artifact handles. Each model retains its own KV cache.

Draft cancellation revokes unclaimed artifacts and recycles their buffers after
local GPU work completes. Claim and revocation are serialized by the Draft Worker:
if cancellation wins, Target cannot obtain a descriptor or start a read.
If claim wins, the source stays immutable until Target's ACK. The Target
claim/read/ACK task survives its HTTP caller's cancellation. Target also ACKs if
it abandons a claimed artifact before starting any read. Target keeps a received
tensor until verification output or request abort. Peer failures or uncertain DMA
completion may still retain storage until process termination. This internal
protocol requires updating frontend and both roles together.

Both roles use the plugin's native-vLLM adapter described in the
[integration contract](mrv2-integration.md). Its runtime patches do not modify
installed vLLM files. The [Rust frontend workflow](frontend-workflow.md)
uses these endpoints for both direct token-input requests and public API dispatch.

## Discovery identity

`GET /status` reports the loaded `model`, nullable `revision`, `tokenizer`,
nullable `tokenizer_revision` and `max_model_len` in addition to the role,
admission state, active sessions, candidate format and token budget. Frontend
registry probes compare these with discovery configuration before admitting the
endpoint to routing. When the model-server loads prepared snapshots, it preserves their configured
source identity and supplies `prepared_tokenizer` metadata for frontend artifact
preparation. Local snapshot paths do not replace the published source identity.
Standalone launches report the engine identity directly.

Router selects a Target only when its scope contains a healthy Draft with the
same candidate format, then restricts Draft selection to that format. Random
sampling excludes token-only roles before selection. These observations come from
readiness probes; the workflow rechecks live status before opening sessions.

## Controller lifecycle

`GET /healthz` and `GET /readyz` check engine health and remain healthy during
drain. This preserves the Kubernetes Service endpoint for existing multi-round
sessions. Admission and discovery independently exclude new bindings.
`POST /v1/internal/admission/close` permanently closes new bindings for this
process and returns telemetry version 2. `GET /v1/internal/telemetry` returns the
same observation without changing admission. `running_requests` counts owned
Draft and Target sessions, including sessions waiting between rounds. Native
scheduler/KV gauges come from EngineCore; `draft_target_workload` contains the
ready/remote-waiting split described below. Request-wide token counters remain
null and latency histograms have no observations. Round measurements are a
separate frontend record, not substitutes for TTFT, TPOT or E2E distributions.

`/status.retained_artifacts` reports Worker-owned proposal/transfer storage.
It is separate from request telemetry and is consumed by the model-server
supervisor during shutdown.

Role discovery uses `GET /status?observations=false`: role/model identity,
admission state, candidate format and budgets are available without a GPU or
EngineCore RPC. `/healthz` and `/readyz` use the same lightweight check. Full
`GET /status` retains native workload and retained-artifact observations for
execution and drain; `/v1/internal/telemetry` publishes the detailed workload.
A slow observation query does not remove a healthy worker from Router selection.

## Observations

Update the frontend and both roles together. Proposal and commit responses carry
`observation`; commits also carry nullable `transfer`. These are measured by the
engine and transport owners, not inferred from returned text or session counts.

### Per-round execution

The batch executor returns a `DraftTargetRoundObservation` beside each output.
It identifies the request, round number, input version, selected endpoints,
confirmed context length and caller batch size. Prefill is round zero and has no
Draft measurement. A binding refusal returns an `admission_closed` round without
proposal or Target measurements, together with its unchanged request continuation.
Completed and cancelled rounds are returned; stream drop or
execution failure records an `aborted` round in tracing and aggregate metrics.

| Measurement | Boundary and meaning |
| --- | --- |
| `dispatch_wait_seconds` | State creation or preceding round completion to execution; includes caller queueing and worker selection. |
| `draft_seconds` | Frontend Draft binding/commit through proposal receipt, including HTTP overhead. |
| `proposal_wait_seconds` | This proposal's completion to submitting the batch; measures waiting for sibling proposals. |
| `target_seconds` | Initial generate or verification submission to receiving this member's result. Includes preparation, native queueing and response overhead. |
| Engine `queue_seconds` | Ready engine work waiting to be scheduled, summed across steps. Excludes remote-candidate waits. |
| Engine `scheduled_seconds` | Sum of native schedule-to-result wall time. Each member observes its shared batch duration; this is not CUDA-event GPU time. |
| Engine `remote_wait_seconds` | Previous Target output to admission of the next candidates (or preemption). Includes caller waiting, drafting and transfer. |
| Engine `steps`, `scheduled_tokens`, `preemptions` | Actual native work in this output interval. `batch_requests` and `batch_tokens` describe its final native step, not the caller's batch. |
| `proposed_tokens` / engine `verified_tokens` / engine `accepted_tokens` / `committed_tokens` | Draft output, candidates actually checked, sampler acceptance before stop truncation, and final Target output respectively. Acceptance excludes correction/bonus tokens. |
| Transfer `bytes`, `read_seconds`, `prepare_seconds` | Actual proposal or KV checkpoint bytes, distinguished by round `phase`; Worker read including readiness fencing/polling; role claim through read, ACK and staging. Null when no tensor transfer is needed. |

These intervals overlap and must not be added. In particular, remote wait starts
in the preceding output interval. Missing stages on cancellation are null, not
zero. Engine measurements are retained only until the owning generator consumes
the output or closes; they are not an engine-side history store.

The existing frontend `/metrics` endpoint exports `foretoken_dt_rounds_total`,
`foretoken_dt_tokens_total`, `foretoken_dt_stage_seconds` histograms, and
`foretoken_dt_transfer_bytes_total`. These are process-level aggregates.
Labels contain only phase, outcome and stage. Request IDs, round numbers and
endpoints remain in `foretoken::dt` traces and returned records. `accepted / verified`
is the sampler acceptance rate; `committed` also includes Target-generated tokens.
Aborted rounds may have incomplete token/transfer observations.

### Online worker workload

`GET /status.workload` and `/v1/internal/telemetry.draft_target_workload` read
EngineCore directly. BackendRegistry retains these timestamped snapshots for
Router scorers through `RouteTargetStats`; it does not scrape Prometheus.
The round API also exposes the selected workers' preparation-time snapshots via
`workloads()`. Measurements describe native scheduler boundaries, not an
instantaneous GPU-kernel activity probe. Registry data ages between readiness
refreshes; use `collected_at_unix_ms` when judging its freshness.

- `running_requests` includes resident requests waiting for remote candidates.
  `ready_requests` excludes those waiters; `remote_waiting_requests` counts them.
- `waiting_requests` counts the native waiting and skipped-waiting queues.
  `waiting_tokens` and `ready_tokens` count known uncomputed tokens, including
  ready candidates. They do not predict future output or subtract possible cache hits.
- `max_num_seqs` is native scheduling capacity. `kv_used_blocks` counts pinned
  device blocks; `kv_capacity_blocks` excludes vLLM's reserved null block.
  Reclaimable prefix-cache blocks are free for allocation even if their contents
  remain in HBM. `kv_cache_usage` has the same native capacity semantics.

### KV migration inputs

Request state exposes `target_location()`, `context_tokens()` and live
`target_kv()`. The latter uses `GET /sessions/{id}/kv`; a fresh request has no
Target, and a session with no current engine allocation reports `kv: null`.
A Draft session between proposals also has no live engine request, even though
unreferenced prefix-cache pages may still exist.

A KV observation contains context length, computed-token frontier and allocated
blocks/bytes per cache group. It comes from native page tables and configured
page sizes. Shared pages count toward each request; lookahead pages can be
allocated before they contain confirmed KV. Consequently these bytes are neither
exclusive memory consumption nor an exact migration payload. An engine-finished request has no live KV. Frontend stop handling can finish
a request after this snapshot; preemption may also invalidate it immediately.
Session location identifies its owner and does not guarantee resident KV.

This supplies location, context, capacity and measured workload for a later
wait-versus-move decision. Explicit migration records use `phase: "migrate"` and
include `migration.source_target_endpoint`, `checkpoint_tokens`, `export_seconds`,
the receiver transfer measurements and its first resumed engine output. Export
time includes checkpoint creation and publication; `target_seconds` covers the
whole migration through that output. Metrics distinguish these records by phase,
with `kv_export` duration and `checkpoint` token stages. These are measurements
of executed moves, not a prediction of future migration cost. The public scheduler
has no migration policy or cost estimator.

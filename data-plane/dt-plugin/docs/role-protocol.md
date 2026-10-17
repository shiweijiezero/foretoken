<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Draft/Target role protocol

Protocol version 9 connects request-level Foretoken routing and independent
Draft/Target roles. Router selects a fixed Draft set and Target. The Target owns
round progression; native schedulers own token budgets, physical batches and KV.
Update frontend and both roles together. No shared stage scheduler or `/work/*`
API is required.

## Routed generation

`POST /generate` receives `request_id`, `draft_endpoints` (one entry currently),
`target_endpoint` (reachable by the Draft for optional context), `token_ids`,
`max_tokens` and ordinary sampling/stop/priority fields described below.
The Target validates protocol, tokenizer, candidate format, context profile and method
limits, then selects a common initialized `tensor_transports` entry: mutually granted same-host GPU IPC first, then RDMA, then ZMQ. It passes the selected `tensor_transport` in Draft `POST /sessions`
and retains it for the request before prefill. RDMA additionally requires
successful local GPU registration and a connection-owned bidirectional GPU proof
before prefill; see the [binding proof](connector-contract.md#rdma-binding-proof). An empty capability list selects
token-only execution only when both roles use that candidate format.
An explicit Draft refusal becomes `admission_closed` before Target prefill. Other
binding errors terminate and clean up the request without selecting another peer.

Kubernetes allows the HTTP control port between DT roles of the same ModelService,
including deployments without RDMA. Tensor channels use dynamically allocated
ZMQ TCP or Mooncake ports within that service; unrelated model services remain isolated.
The managed deployment supplies Pod IP and existing platform RDMA allocations.

`RoleCommunication` owns the shared HTTP pool and peer protocol; each
`DraftPeerSession` retains its request's control stream. `TargetCoordinator` uses
that session handle and advances the exact confirmed prefix. Native Target prefill supplies the first token and next ticket.
Each subsequent round commits confirmed deltas, selects one chain length from its configured default and remaining output,
prepares its data and submits the current ticket once.
`TargetCoordinator.select_candidate_count()` selects K without owning execution;
its default also respects the bound method limit and Target capacity. The role consumes ticket
ownership before the native utility call; uncertain admission is never retried.
A stale ticket returns false and the coordinator consumes native replay output.
Unused proposal artifacts are revoked. Native preemption owns KV recovery.

The NDJSON stream starts with `opened` and its session ID, before RDMA binding
preparation. Requests borrow the connection-owned proof; cancellation closes their sessions
without cancelling shared probes. Prefill
still waits for successful preparation. `committed` contains
confirmed token/text deltas, nullable next version, native observations, transfer
measurements and the Target-owned round record. Protocol v9 identifies the actual
RDMA transport incarnation and uses connection-owned proof endpoints. It retains nullable
`round.admitted` for ticket admission and `round.context_transfer` for the
Target-to-Draft feature read; `round.transfer` retains proposal-read measurements. Keep the stream open for session
ownership. The frontend closes the session explicitly on completion/cancellation;
Target closes its Draft and joins native admission, abort and started operations.
Admission includes asynchronous input preprocessing: close waits until submission
finishes before aborting and fencing GPU work, including checkpoint consumption. A failed
cleanup remains visible through `GET /sessions/{id}/state` (`open`, `closing`,
`closed`, `failed`). A claimed source artifact still requires the consumer ACK.

Each proposal and native submission carries
`{request_id, work_id, frontier, round, phase}`. Native `physical_batches` report
batch ID, wall-clock start/end, scheduled tokens and members' engine/client request
IDs, confirmed frontier and work identity. No logical batch is assigned above the
native scheduler. Readiness and ticket admission are distinct from verification's
token acceptance. Native workload snapshots are observations, not adapter credits.

## Candidate policy and capacity

Protocol v9 advertises two distinct nullable length fields, separately from tensor transport:

| Role | `default_draft_tokens` | `max_candidate_tokens` |
| --- | --- | --- |
| Target | Positive round default; must fit capacity | Native engine capacity, fixed at startup |
| Ordinary autoregressive Draft | null | null; native context/resource limits still apply |
| Neural Draft | null | Native proposal block length; the adapter can publish a shorter prefix |

Target chooses the round count, capped by remaining output, the bound Draft
method limit and its own engine capacity. Router and live binding check execution
compatibility; the two capacities need not match. Manual callers may vary candidate counts
within the advertised capacity. Neural prefix truncation applies to both token IDs
and proposal probability rows. No adaptive K policy is enabled by this contract.

## Manual role diagnostics

The direct proposal and verification endpoints remain available for diagnostics.
An unbound `/generate` (without `draft_endpoints`) exposes manual ticket control.
`/verify` prepares and admits one member. It rejects bound coordinated requests
with HTTP 409, preserving their sole round owner. Each request can become ready
independently; native scheduling forms batches.

The model-server can also refuse a new Draft session with HTTP 503 and
`error.code: "admission_busy"` before acquiring its permit. Target terminates and cleans up the request on ambiguous Draft binding failure;
model services do not choose another peer. A manual caller may select another Draft only
after explicit refusal proves nonacceptance. Other HTTP or transport errors are fatal.

1. Open Target `POST /generate` with `token_ids` and `max_tokens`, optionally
   `stop` strings, `stop_token_ids`, `min_tokens`, `ignore_eos`, `priority` and `sampling`.
   `sampling` contains `temperature`, `top_p`, `top_k` and optional Target `seed`.
   Read its NDJSON response. The first `opened` event supplies a
   `session_id`. Each `committed` event carries a token delta, text delta,
   `finished`, `finish_reason`, `stop_reason`, `cached_token_count` and the next
   `version` (null on termination). A draining Target instead returns
   `{"event":"admission_closed"}` before creating any session or running prefill.
   The caller may then select another Target.
2. After the first nonterminal commit, open Draft `POST /sessions` with the
   original prompt plus confirmed tokens, the Target's `version`, and the same
   `sampling` and `priority` settings, plus a common `tensor_transport` when using
   tensors. Omitting the selection uses the Draft's preferred initialized path; manual
   callers must ensure the Target supports it. Draft uses independent random draws, ignoring the Target
   seed. Read the
   `opened` event and **keep this response stream open** throughout the session.
   A draining Draft instead returns `{"event":"admission_closed"}` and closes
   the NDJSON body. This guarantees no session or proposal was created. Admission
   is decided when the body starts, including if drain happened after HTTP headers.
   The caller may keep the Target session and select another Draft. A missing/malformed response
   or HTTP error does not provide the same guarantee and is not retried.
3. Call Draft `POST /sessions/{id}/propose` with that `version` and `max_tokens`
   within the method limit, if any, and native context limits. Its result contains `version`, `token_ids`, and
   `artifact`: null without tensor transport, otherwise `{artifact_id}`. This handle does not
   expose a GPU address.
4. Call Target `POST /sessions/{id}/verify` with that version, candidate IDs,
   `artifact`, and the selected Draft HTTP `source_endpoint`. With tensor transport Target
   first calls Draft `POST /artifacts/{artifact_id}/claim` to obtain the GPU
   `PayloadRef` defined in the [Connector contract](connector-contract.md).
   A revoked or already claimed artifact returns HTTP 409; Target propagates
   that failure without starting a read or consuming its ticket. Otherwise, Target
   completes the transfer into GPU storage and sends `POST /artifacts/{artifact_id}/release` to
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
caller must not re-tokenize display text to reconstruct commits: stop handling
can make displayed text and internal token boundaries differ. This first adapter
supports text and linear candidates and does not negotiate tokenizer compatibility; callers
must select models with matching token-ID semantics.

## Target context for neural Drafts

For a neural Draft, `/status` advertises a non-null `context_profile` identifying
the method and Target/Draft model pair. Managed roles use preparation's frozen
checkpoint revisions in this profile, while discovery retains the requested model
identity. Standalone roles use the loaded native config's commit identity; local
checkpoint paths must identify the same immutable weights. Selected roles must agree on this profile.
`propose` additionally carries `context_source: {endpoint, session_id}` for the
current Target. Independent token-only Draft models ignore this field.

Draft calls Target `POST /sessions/{id}/context` with the same `version` and
selected `tensor_transport`. Target checks that path against its capabilities and
any coordinated binding. Target
validates the paused engine ticket and returns `artifact_id`, the complete confirmed
`token_ids`, and `profile`. The artifact contains a dense
`[computed_tokens, feature_layers, hidden_size]` tensor in the Target model dtype;
the first layer is the final hidden state and subsequent layers are native auxiliary
outputs. The computed prefix excludes the final anchor token. Draft checks the
profile, prefix and dimensions, then uses the existing claim/read/ACK endpoints.
The proposal owns the received features until native execution finishes or is aborted.
Context transfer completes outside the Target compute loop, allowing other requests
to execute while this request waits for Draft.
If Target revokes that context before Draft admission, Draft returns HTTP 409 with
`detail: "Target context was revoked"`. The managed coordinator preserves the
confirmed frontier and consumes Target's native replay output next. Other proposal
conflicts and transport failures remain errors; no submitted transfer is replayed.

## Target KV checkpoint and resume

Targets started with `--tensor-transport rdma --enable-kv-migration` advertise
`kv_migration: true`; checkpoint DMA requires the strict RDMA startup path.
This manual diagnostic requires an unbound session; routed requests reject
checkpoint export. The caller must pause between rounds and select a compatible destination.

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

The diagnostic caller checks model/tokenizer identities and matches the exported
prefix before calling `/resume`. Callers
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
candidate format, `default_draft_tokens` and `max_candidate_tokens`. A failed EngineCore makes status and new
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

Both roles advertise `token_ids_log_probs` with ZMQ or RDMA, or
`greedy_token_ids` without tensor transport. `/status.tensor_transports` lists
initialized paths (`gpu_ipc`, `rdma`, `zmq`), or is empty for token-only. Router requires an
compatible intersection; GPU IPC requires a same-host, symmetric peer relation
from `/status.gpu_ipc: {host_id, device_id, peer_devices}`. Target selects and logs the request binding. Default auto offers ZMQ TCP, allocated RDMA and supported MACA peers.
NVIDIA GPU IPC is not enabled. Missing granted peer access excludes that path;
forced `--tensor-transport gpu-ipc` fails startup instead. Explicit IPC requires shared same-host paths. Failures
never switch an existing artifact to another backend or replay a read.
Random sampling requires tensor transport. Tensor data moves directly between workers. Target coordinates candidate IDs and artifact handles; Frontend receives confirmed output. Each model retains its own KV cache.

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
admission state, active sessions, candidate format, `default_draft_tokens` and `max_candidate_tokens`. Frontend
registry probes compare these with discovery configuration before admitting the
endpoint to routing. When the model-server loads prepared snapshots, it preserves their configured
source identity and supplies `prepared_tokenizer` metadata for frontend artifact
preparation. Local snapshot paths do not replace the published source identity.
Standalone launches report the engine identity directly.

Router selects a Target only when its referenced pools contain a healthy Draft
with the same candidate format and context profile whose method supports the
Target default candidate count. Ordinary autoregressive Drafts have no static K limit. Random sampling excludes
token-only roles. Router selects the compatible Draft through the same scorer/picker pipeline.
Target checks live tokenizer, context and candidate capabilities before binding.
Role services never choose another peer.

## Controller lifecycle

Managed roles expose the Rust model-server ingress, which owns instance admission,
controller-projected limit updates, RequestLedger execution ownership and session
permits. Python listens on loopback behind that ingress. `/generate` and
`/resume.generation` carry the Target reservation; the Rust ingress consumes it
before forwarding the request. Draft session creation has no Target reservation.
Both session types carry native engine `priority`. Accepted sessions retain their
instance permit through completion or cancellation.

`GET /v1/internal/admission` and `/v1/internal/admission/ready` report the applied
instance configuration and its readiness. Standalone Python launches do not
provide controller-managed admission or a shared request ledger.

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
admission state, candidate format and length capabilities are available without a GPU or
EngineCore RPC. `/healthz` and `/readyz` use the same lightweight check. Full
`GET /status` retains native workload and retained-artifact observations for
execution and drain; `/v1/internal/telemetry` publishes the detailed workload.
A slow observation query does not remove a healthy worker from Router selection.

## Observations

Update the frontend and both roles together. Proposal and commit responses carry
`observation`; commits also carry nullable `transfer`. These are measured by the
engine and transport owners, not inferred from returned text or session counts.

### Per-round execution

Target attaches a `DraftTargetRoundObservation` to each confirmed output.
Its nullable `tensor_transport` identifies the fixed binding; null means token-only. It
identifies the request, round, input version, selected endpoints and confirmed
context length. Prefill is round zero with no Draft measurement. `batch_requests` describes the
final native step, while `physical_batches` preserve actual memberships. Failed or
cancelled requests can end without a final round record.

| Measurement | Boundary and meaning |
| --- | --- |
| `draft_seconds` | Proposal request through receipt, including HTTP and native queueing. |
| `target_seconds` | Initial prefill or verification preparation through confirmed output and Draft prefix synchronization; includes native queueing. |
| Engine `queue_seconds` | Ready engine work waiting to be scheduled, summed across steps. Excludes remote-candidate waits. |
| Engine `scheduled_seconds` | Sum of native schedule-to-result wall time. Each member observes its shared batch duration; this is not CUDA-event GPU time. |
| Engine `remote_wait_seconds` | Previous Target output to admission of the next candidates (or preemption). Includes caller waiting, drafting and transfer. |
| Engine `steps`, `scheduled_tokens`, `preemptions` | Actual native work in this output interval. `batch_requests` and `batch_tokens` describe its final native step, not the caller's batch. |
| `requested_tokens` | Target-selected K before Draft execution; null for prefill. |
| `proposed_tokens` / engine `verified_tokens` / engine `accepted_tokens` / `committed_tokens` | Draft output, candidates actually checked, sampler acceptance before stop truncation, and final Target output respectively. Acceptance excludes correction/bonus tokens. |
| Transfer `transport`, `bytes`, `read_seconds`, `prepare_seconds` | Concrete completed-read backend and actual proposal bytes; Worker read including readiness fencing/polling; role claim through read, ACK and staging. Null when no tensor transfer is needed. |

These intervals overlap and must not be added. In particular, remote wait starts
in the preceding output interval. Unavailable measurements are null. Engine measurements are retained only until the owning generator consumes
the output or closes; they are not an engine-side history store.

The existing frontend `/metrics` endpoint exports `foretoken_dt_rounds_total`,
`foretoken_dt_tokens_total`, `foretoken_dt_stage_seconds` histograms, and
`foretoken_dt_transfer_bytes_total`. These are process-level aggregates.
Labels contain the logical model name, phase, outcome and stage. Request IDs, round numbers and
endpoints remain in `foretoken::dt` traces and returned records. `accepted / verified`
is the sampler acceptance rate; `committed` also includes Target-generated tokens.
Metrics cover received round observations; failed streams can have incomplete
token/transfer totals.

### Online worker workload

`GET /status.workload` and `/v1/internal/telemetry.draft_target_workload` read
EngineCore directly. BackendRegistry retains these timestamped snapshots for
Router scorers through `RouteTargetStats`; it does not scrape Prometheus.
Measurements describe native scheduler boundaries, not an
instantaneous GPU-kernel activity probe. Registry data ages between readiness
refreshes; use `collected_at_unix_ms` when judging its freshness.

- `running_requests` includes resident requests waiting for remote candidates.
  `ready_requests` excludes those waiters; `remote_waiting_requests` counts them.
- `waiting_requests` counts the native waiting queues (including KV-holding waiters on NVIDIA
  and skipped waiters on the pinned MetaX core).
  `waiting_tokens` and `ready_tokens` count known uncomputed tokens, including
  ready candidates. They do not predict future output or subtract possible cache hits.
- `max_num_seqs` is native scheduling capacity. `kv_used_blocks` counts pinned
  device blocks; `kv_capacity_blocks` excludes vLLM's reserved null block.
  Reclaimable prefix-cache blocks are free for allocation even if their contents
  remain in HBM. `kv_cache_usage` has the same native capacity semantics.

### KV migration inputs

`GET /sessions/{id}/kv` reports native residency for a live Target session. A
session with no current engine allocation reports `kv: null`. Draft sessions
between proposals have no live engine request, although cached prefix pages may
remain. The diagnostic caller owns the session's location and confirmed tokens.

A KV observation contains context length, computed-token frontier and allocated
blocks/bytes per cache group from native page tables and configured page sizes.
Shared pages count toward each request; lookahead pages can be allocated before
they contain confirmed KV. These bytes are neither exclusive memory consumption
nor an exact migration payload. Preemption can invalidate the snapshot immediately.
The internal checkpoint/resume operations do not publish coordinated migration
rounds or implement a public migration policy.

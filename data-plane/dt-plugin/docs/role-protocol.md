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
   `version` (null on termination).
2. After the first nonterminal commit, open Draft `POST /sessions` with the
   original prompt plus confirmed tokens, the Target's `version`, and the same
   `sampling` settings. Draft uses independent random draws, ignoring the Target
   seed. Read the
   `opened` event and **keep this response stream open** throughout the session.
3. Call Draft `POST /sessions/{id}/propose` with that `version` and `max_tokens`
   within the role budget. Its result contains `version`, `token_ids`, and
   `artifact`: null without RDMA, otherwise `{artifact_id, payload}`. `payload`
   is the GPU `PayloadRef` descriptor defined in the [Connector contract](connector-contract.md).
4. Call Target `POST /sessions/{id}/verify` with that version, candidate IDs,
   `artifact`, and the selected Draft HTTP `source_endpoint`. In RDMA mode Target
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

## Concurrency and lifecycle

Each session allows one outstanding proposal. Draft commits during an active
proposal and mismatched context versions return HTTP 409. Unknown sessions return
404; malformed messages and invalid token IDs return 422. Generation failures
after response headers terminate the stream; the frontend must cancel the paired
session rather than treating end-of-stream without a terminal event as success.

`GET /status` reports model, role, acceptance state, active-session count,
candidate format and token budget. A failed EngineCore makes status and new
session admission return 503. `POST /drain` rejects new sessions with 503
but allows existing proposals, commits and verification to finish. Drain is
irreversible for that service process. Deployment automation must wait for zero
active sessions and zero `retained_artifacts` before terminating the role. Cancelling
sessions does not prove transfer storage is released. There is no
transparent session migration or replay after a process restart.

A waiting Target stays in vLLM's request state but is not scheduled for model
execution. Ready requests can continue to batch. The Draft implementation uses
ordinary per-round requests and automatic prefix caching, so its unfilled tail
may be recomputed; it does not promise optimal Draft KV reuse.

## Transport and extension boundary

Both roles advertise `token_ids_log_probs` when started with RDMA, or
`greedy_token_ids` without it. The selected pair must match. Random sampling
requires RDMA. Tensor data moves directly between workers; frontend relays only
candidate IDs and immutable descriptors. Each model retains its own KV cache.

The Target read/ACK task survives its HTTP caller's cancellation. Source
publication remains registered until ACK; Target keeps its received tensor until
verification output or request abort. A transfer failure may retain storage until
process termination. See the [Connector contract](connector-contract.md).

RDMA execution requires the separate vLLM extension on both roles. Without RDMA,
Target still needs the external-candidate extension. No role patches engine
methods during installation. The [Rust frontend workflow](frontend-workflow.md)
uses these endpoints for both direct token-input requests and public API dispatch.

## Discovery identity

`GET /status` reports the loaded `model`, nullable `revision`, `tokenizer`,
nullable `tokenizer_revision` and `max_model_len` in addition to the role,
admission state, active sessions, candidate format and token budget. Frontend
registry probes compare these with discovery configuration before admitting the
endpoint to routing. This is engine metadata, not a new caller-selected identity.

## Controller lifecycle

`GET /healthz` and `GET /readyz` check engine health and remain healthy during
drain. This preserves the Kubernetes Service endpoint for existing multi-round
sessions. Admission and discovery independently exclude new bindings.
`POST /v1/internal/admission/close` permanently closes new bindings for this
process and returns telemetry version 2. `GET /v1/internal/telemetry` returns the
same observation without changing admission. `running_requests` counts owned
Draft and Target sessions, including sessions waiting between rounds. Native
scheduler/KV gauges and token counters remain null; latency histograms have no
observations. Controllers must not interpret those fields as measured zero load.

`/status.retained_artifacts` reports Worker-owned proposal/transfer storage.
It is separate from request telemetry and is consumed by the model-server
supervisor during shutdown.

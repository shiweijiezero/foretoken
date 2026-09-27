<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Greedy role protocol

This internal protocol connects a frontend coordinator to independently deployed
models. Role endpoints execute model work; the frontend owns placement, stage
ordering, stream lifetime and cancellation. Local vLLM schedulers form batches
and own KV allocation. A version is a Target engine generation ticket, not a KV
length or a transport-buffer identity.

## One request

1. Open Target `POST /generate` with `token_ids` and `max_tokens`, optionally
   `stop` strings, `stop_token_ids`, `min_tokens` and `ignore_eos`. Read its NDJSON response. The first `opened` event supplies a
   `session_id`. Each `committed` event carries a token delta, text delta,
   `finished`, `finish_reason`, `stop_reason`, `cached_token_count` and the next
   `version` (null on termination).
2. After the first nonterminal commit, open Draft `POST /sessions` with the
   original prompt plus confirmed tokens and the Target's `version`. Read the
   `opened` event and **keep this response stream open** throughout the session.
3. Call Draft `POST /sessions/{id}/propose` with that `version` and `max_tokens`
   within the role budget. Its JSON result contains `version` and `token_ids`.
4. Call Target `POST /sessions/{id}/verify` with that version and candidate IDs.
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
supports greedy text only and does not negotiate tokenizer compatibility; callers
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
active sessions, or cancel their owners, before terminating the role. There is no
transparent session migration or replay after a process restart.

A waiting Target stays in vLLM's request state but is not scheduled for model
execution. Ready requests can continue to batch. The Draft implementation uses
ordinary per-round requests and automatic prefix caching, so its unfilled tail
may be recomputed; it does not promise optimal Draft KV reuse.

## Transport and extension boundary

See the [method payload contract](connector-contract.md) for the proposed
probability, feature and tree boundaries; these are not enabled role APIs.

Greedy candidates are short token-ID lists on the HTTP control plane. This path
has no cross-model KV copy and no Mooncake payload reference. The independent
Mooncake tensor API remains available for tensor-bearing methods but is not wired
into these messages. Adding stochastic proposals requires the corresponding
proposal distributions and verifier semantics, not just an extra JSON field.

The Target requires the separate vLLM external-speculation extension. The adapter
imports its public output ticket and submission API without modifying engine
methods. The [Rust frontend workflow](frontend-workflow.md) consumes this protocol through
both the token-input example and the normal Router/public API path when supplied
with controller-generated DT serving snapshots. Role deployment and drain use
the existing Group lifecycle. Metric-driven autoscaling has not been validated.

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

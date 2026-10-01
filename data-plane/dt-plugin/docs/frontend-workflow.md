<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Frontend Draft/Target execution

The Rust frontend owns the Draft/Target round loop. `foretoken-llm-facade` owns
HTTP role sessions and response decoding; `foretoken-server::draft_target`
orders proposals, Target verification and confirmed-prefix updates. Each model's
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
   |<-- candidate IDs + distribution descriptor ----------------|
   |-- verify(ticket, IDs, descriptor) --->|                    |
   |                                      |<== Mooncake log(q) =|
   |                                      |-- read ACK ------->|
   |<-- confirmed delta + next generation |                    |
   |-- commit(delta, next generation) ------------------------->|
```

The frontend only relays descriptors, never probability arrays. Target admits
verification after its Worker completes the read. Its read/ACK task retains
transport ownership even if the frontend disconnects.

The stream owns both HTTP response bodies through every await. Dropping the
stream cancels the sessions even while a proposal is outstanding. Terminal output
releases them before being yielded, so retaining an exhausted stream cannot
retain a remote session. An EOF without a terminal commit is a protocol failure.
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

Registry health probes check the actual role, weight identity, tokenizer identity,
candidate format and admission state. Both engines must report the service's
configured tokenizer and revision. For local snapshots the engine revision is
null; local paths must resolve to the intended artifacts on every host. Equal
path names alone do not establish equal file contents. This first implementation
requires a common tokenizer; different-tokenizer speculation is unsupported.

The existing Filter–Scorer–Picker pipeline selects Target first and Draft next
within that scope. Each replica has its own route ID and load reservation.
Selection does not release Target's reservation: both roles remain reserved until
terminal output, cancellation or failure. There is one selected Draft per request;
multiple available replicas are not multi-Draft speculation. DT has no KV-index
source or engine scheduler telemetry yet, so routing uses frontend reservations
without inventing cache hits or scheduler observations.

`RuntimeBuilder` reuses the existing tokenizer, model publication, request deadline
and public API handlers. A successfully published DT model appears in `/v1/models`;
text/chat generation dispatches through the DT workflow. Invalid snapshot updates
leave the active generation intact. Draining a role removes it from subsequent
selections after readiness refresh while existing sessions retain their owner.
There is no migration or replay of an active request onto another replica.

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
RDMA allocation enables the Worker connector; Pod IP supplies its handshake address. See the
[deployment example](../../../examples/draft-target/README.md).

The role reports owned session counts, not inferred scheduler or KV metrics.
Metric-driven autoscaling remains unvalidated. This path requires the plugin and
the exact native vLLM version in the [integration contract](mrv2-integration.md).

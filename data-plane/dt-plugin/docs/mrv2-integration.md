<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# MRV2 integration contract

The DT role service requires the independent vLLM `feat/external-speculation`
branch ([PR #1](https://github.com/shiweijiezero/vllm/pull/1)), based on
`3b4566c5cf014605de6aeab6eb831b4f20511c17`. The pinned
submodule is unchanged. Installing this package does not patch vLLM, replace
engine methods, or register a `vllm serve` entry point.

## Engine boundary

The extension adds `method="external"` and
`AsyncLLM.submit_external_draft_tokens(ticket, token_ids)`. A nonterminal
`RequestOutput.external_draft_request` carries an immutable request/generation
ticket after normal output stop handling. Submission returns whether the ticket
was admitted, not whether verification accepted the candidates. Stale or consumed
tickets return false; an empty candidate list requests a Target-only step.

| Owner | Responsibility |
| --- | --- |
| Scheduler | Wait per request, admit ready candidates, account for verification work, invalidate tickets on preemption |
| EngineCore | Receive submissions through the existing utility queue; sleep when only external waiters remain |
| MRV2 | Resolve current request slots, fill GPU draft-token state before input preparation, consume staged GPU proposal distributions through native rejection sampling |
| OutputProcessor | Attach the next ticket after stop handling; never issue one on terminal output |
| Foretoken frontend | Select role instances, order rounds, retain session lifetimes, stream confirmed output |
| Role service | Translate session/version messages to engine requests and tickets; abort owned work on disconnect |

No local Draft model or speculator is constructed by the Target engine. Updating
Scheduler's CPU candidates alone is insufficient: MRV2 input preparation reads
`req_states.draft_tokens`, so the extension fills that state after request-slot
updates. EngineCore distinguishes lifecycle ownership from schedulable work to
avoid stepping continuously while remote candidates are outstanding.

This mode supports text, one worker, eager execution, synchronous local scheduling
and output interval one. `draft_sample_method="probabilistic"` consumes external
proposal distributions; the token-only mode requires greedy sampling. RDMA Draft
also needs the extension's processed-logit export. Remote waits are per request;
other requests can still execute. Distributed model execution, CUDA graphs,
multimodal input and KV/EC transfer are not enabled by this extension.

The role CLI sets `VLLM_BATCH_INVARIANT=1` before importing vLLM unless the
process environment already specifies a value. Native kernels own this numerical
policy; the plugin does not replace their implementations. Target-only comparison
runs must use the same setting. Model/backend support and hardware restrictions
still apply, and independent Draft randomness prevents a general seeded-sequence
equality claim. See the [role startup requirements](../README.md#start-two-roles).

## Frontend and role ownership

One request binds one Draft and one Target. Target prefill emits the first
confirmed token, then the frontend repeats:

1. Align Draft with the exact Target-confirmed prefix.
2. Ask Draft for candidates within the remaining output budget.
3. Submit candidates using the Target's current ticket.
4. Stream the actual Target delta and stop outcome.

Each engine owns local batching and KV state. Draft rounds are ordinary vLLM
requests against the confirmed prefix, using native prefix caching. Target owns
verification and final token commitment. The normal frontend output processor
retains text decoding and stop-string handling; it does not reconstruct engine
token state by re-tokenizing cropped text.

Dropping the frontend stream closes both role sessions. Draining rejects new
bindings while allowing existing rounds to finish. Engine health/readiness stays
available during drain so Service routing does not interrupt existing sessions.
Frontend or role failure terminates the request; transparent migration and replay
are not implemented. Multiple Draft replicas provide capacity, not collaborative
candidate trees or intermediate verification.

## Worker and transport boundary

`DraftTargetWorkerExtension` is installed through the native `worker_extension_cls`
entry point. It connects MRV2 GPU I/O hooks to Mooncake. There is no Foretoken or
Mooncake import in the generic engine extension.

Draft marks each per-round request with `SamplingParams.extra_args["dt_artifact_id"]`.
The sampler exposes processed logits, and the Worker captures complete float32
`log(q)` rows without copying probabilities to the API process. Worker RPCs
publish source buffers, start/poll destination reads, stage received artifacts
against engine tickets, and return buffers to an exact-shape pool after their
owners finish using them. Registrations remain until role shutdown; idle buffers
are reusable and excluded from retained-artifact counts.

Target's role waits for read readiness, ACKs the source, stages the tensor and
submits candidates through the existing EngineCore utility queue. MRV2 maps the
staged distribution to current GPU request slots before native rejection sampling.
Submission success is not consumption completion: the role retains Target storage
until subsequent engine output or abort. See the
[Connector contract](connector-contract.md) for scaling and temperature semantics.

`MooncakeTransport` owns registrations; `RegisteredTensor` fences producer writes
and the final local consumer with CUDA events. RDMA completion is independent of
CUDA producer readiness. A completed-read ACK permits source release, not reuse
of a destination still in use by model execution. Uncertain buffers remain
retained until process termination. The standalone diagnostic exercises the
transport separately and does not establish inference correctness or performance.

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# MRV2 integration contract

The DT role service requires the independent vLLM `feat/external-speculation`
branch, based on `1be36283678a9a94fc8fdaad6c95c2896d6b4015`. The pinned
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
| MRV2 | Resolve current request slots, fill GPU draft-token state before input preparation, reuse greedy rejection sampling |
| OutputProcessor | Attach the next ticket after stop handling; never issue one on terminal output |
| Foretoken frontend | Select role instances, order rounds, retain session lifetimes, stream confirmed output |
| Role service | Translate session/version messages to engine requests and tickets; abort owned work on disconnect |

No local Draft model or speculator is constructed by the Target engine. Updating
Scheduler's CPU candidates alone is insufficient: MRV2 input preparation reads
`req_states.draft_tokens`, so the extension fills that state after request-slot
updates. EngineCore distinguishes lifecycle ownership from schedulable work to
avoid stepping continuously while remote candidates are outstanding.

This engine mode supports greedy text, one worker, eager execution, synchronous
local scheduling and output interval one. Remote waits are per request; other
requests can still execute. Stochastic proposals, distributed execution, CUDA
graphs, multimodal input and KV/EC transfer are not enabled by this extension.

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

## Transport boundary

Greedy candidates are token IDs transported by the internal HTTP control API.
The Mooncake tensor transport is currently consumed by the diagnostic CLI, not
the inference loop; this path does not transfer KV between models.

`MooncakeTransport` owns its native engine and persistent registrations.
`RegisteredTensor.publish()` fences production and creates one immutable export;
`read()` pulls into receiver-owned storage asynchronously. The control owner
sends a completed-read acknowledgement, then `release_source()` permits reuse.
`close()` fences local GPU consumption before unregistering.

`publish`, `read`, and buffer `close` accept a keyword-only `ready_event`. The
Worker/Runner must record it on the tensor device after joining **all** local
streams that use that storage. Publication waits for producer writes; a read waits
for previous destination use; close waits for the final local consumer. The event
must not be re-recorded during the operation. An unrecorded event is rejected.
Omitting it retains the device-wide fence. The diagnostic exercises recorded
events; integration with actual model streams remains outstanding. This follows
[PyTorch event synchronization](https://docs.pytorch.org/docs/2.11/generated/torch.cuda.Event.html):
the host waits for captured work, rather than enqueueing a CUDA stream wait and
incorrectly assuming that a separately submitted NIC transfer will obey it.

RDMA completion remains a separate boundary: await the native read result before
submitting destination consumers. A producer event cannot establish RDMA
completion, and source ACK cannot establish completion of destination compute.
Failed/uncertain
buffers remain retained until process termination. Buffer pooling, frontend
messages, model/session state, and inference orchestration are not implemented by
the diagnostic. Do not advertise it as a complete DT service.

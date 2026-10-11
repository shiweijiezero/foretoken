<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# MRV2 integration contract

The DT role service targets native vLLM `0.31.0`
(tag commit `db9527a46873454610df6dbedf79a36d6bf1a7f6`) on NVIDIA. MetaX uses the
core/plugin pair and ordered patches in
[`source-environment.json`](../../../deploy/inference-engines/vllm-metax/source-environment.json).
Its FlashAttention metadata builder uses MRV2's maximum sequence-length bound;
MRV2 does not supply the older runner's CPU sequence-length tensor. Exact
per-request lengths remain in the device metadata. Foretoken owns the
compatibility layer in `foretoken_dt.vllm`; it does not edit installed vLLM files
or require a separate engine branch.

Native `worker_cls` and `scheduler_cls` select plugin subclasses. Platform
configuration selects its Worker first; DT then wraps that class, preserving
MetaX's optional precision-debug Worker. The
`vllm.general_plugins` entry point installs process-local EngineCore hooks for candidate admission,
readiness while requests wait for remote candidates, and direct observations
through the existing utility queue. Worker initialization temporarily replaces
the Runner, speculator and sampler factories, restoring each binding afterward.
No upstream scheduling or model-execution method body is copied. These internal
boundaries are version-sensitive, so role startup rejects other vLLM versions.

## Image ownership

The normal [engine environment](../../../deploy/inference-engines/vllm/source-environment.json)
uses the official vLLM image. Its CUDA runtime and Foretoken's Mooncake dependency
preparation supply the NVIDIA transport environment. MetaX retains its own runtime
build. The [model-server image](../../model-server/Dockerfile) installs the DT
distribution and its `vllm.general_plugins` entry point in the engine's Python
environment. The launcher extends inherited plugin allowlists before spawning
EngineCore. Source delivery carries the same package; no runtime source mount is
required.

Ordinary Foretoken engine backports use the shared version-selected patch series.
DT does not maintain an additional engine image or ready-metadata patch.

## Engine boundary

The plugin configures `method="external"` and exposes
`ExternalAsyncLLM.submit_external_draft_batch([(ticket, token_ids), ...])`.
All entries reach the scheduler in one utility call, returning one admission result
per entry; native scheduling still chooses physical GPU batches. A nonterminal
`RequestOutput.external_draft_request` carries an immutable request/generation
ticket after normal output stop handling. Each result reports whether its ticket
was admitted, not whether verification accepted the candidates. Stale or consumed
tickets return false; an empty candidate list requests a Target-only step.

| Owner | Responsibility |
| --- | --- |
| Scheduler | Wait per request, admit ready candidates, account for verification work, invalidate tickets on preemption |
| EngineCore | Receive submissions through the existing utility queue; sleep when only external waiters remain |
| MRV2 | Resolve current request slots, fill GPU draft-token state before input preparation, consume staged GPU proposal distributions through native rejection sampling |
| Plugin AsyncLLM | Attach the next ticket after native output stop handling; never issue one on terminal output |
| Foretoken frontend | Select role instances, order rounds, retain session lifetimes, stream confirmed output |
| Role service | Translate session/version messages to engine requests and tickets; abort owned work on disconnect |

No local Draft model is constructed by the Target engine. Updating
Scheduler's CPU candidates alone is insufficient: MRV2 input preparation reads
`req_states.draft_tokens`, so the plugin fills that state after request-slot
updates. EngineCore distinguishes lifecycle ownership from schedulable work to
avoid stepping continuously while remote candidates are outstanding.

Tickets pair vLLM's unique internal request ID with its cumulative confirmed output
token count. The client retains the internal ID from the native output collector;
no cross-process output schema is changed. Waiting requests remain in the native
running queue and remain eligible for KV preemption. Preemption removes their
waiting state; replay advances the output frontier before a new ticket is issued.

This mode supports text, one worker, eager execution, synchronous local scheduling
and output interval one. `draft_sample_method="probabilistic"` consumes external
proposal distributions; the token-only mode requires greedy sampling. RDMA Draft
also uses the plugin sampler's processed-logit export. Remote waits are per request;
other requests can still execute. Distributed model execution, CUDA graphs,
multimodal input and EC transfer are not enabled by this adapter. The optional
NVIDIA Target migration connector supports one full-attention KV group; other KV
connectors remain unsupported.

The role CLI defaults `VLLM_BATCH_INVARIANT` to `1` for NVIDIA and `0` for MACA
before importing vLLM, unless the process environment already specifies a value.
Native kernels own this numerical
policy; the plugin does not replace their implementations. Target-only comparison
runs must use the same setting. Model/backend support and hardware restrictions
still apply, and independent Draft randomness prevents a general seeded-sequence
equality claim. See the [role startup requirements](../README.md#start-two-roles).

## Neural Draft context

With a neural Draft profile, Target retains a position-indexed history of final and
auxiliary hidden states through a non-model speculator. Export runs on the
EngineCore scheduler thread and selects only the confirmed computed prefix,
excluding the last correction/bonus token. Rejected suffixes are discarded;
request completion, abort and native request removal release the retained history.
Target APC is disabled so a cache hit cannot leave gaps in that history.

The frontend passes the current Target session address with each proposal.
Draft claims and reads the context before admitting a normal native request
whose prompt excludes the anchor token. Its loader keeps the Target backbone on
the meta device and materializes shared embedding/output weights. Native MRV2
loads the selected native speculator and allocates its own KV pages. The feature
forward gathers staged rows for the locally scheduled prefill chunk; sampling supplies
the confirmed anchor. Native speculator execution and request postprocessing
remain unchanged. The adapter defers output copying until the native proposal
exists, returning that chain as the terminal result of the ordinary request.
Partial prefill chunks return no tokens. DFlash/DSpark's full immutable Draft prefix pages
remain eligible for native APC. EAGLE/MTP KV at position t also consumes token t+1;
a page's final row can therefore depend on an anchor outside its hashed prompt.
These methods disable Draft APC. No Target page index crosses the role boundary.
Native profiling and warmup use synthetic features without remote artifact bindings;
output normalization weights are materialized for Targets that apply normalization
inside `compute_logits`.

Features use the existing artifact claim/read/ACK lifecycle. Each round currently
copies the full computed prefix, including when Draft APC skips its first rows.
This is a correctness baseline with potentially substantial repeated transfer cost.
The adapter enables DFlashDraftModel, Qwen3DSparkModel, EAGLE/EAGLE3 and
single-module MTP. Multi-module MTP, HC residuals and
Target-shared sparse indices remain excluded. Gemma4 needs Target KV sharing.
Other DFlash and DSpark architectures require separate integration.
DSpark reuses the native anchor layout, Markov sampling and reduced-vocabulary
mapping. Export reads the native `draft_logits` cache after Markov bias and any
`dspark_draft_topk` truncation, so each transferred q row is conditioned on the
preceding sampled candidates. Remote roles reject `enable_adaptive_verification`:
its confidence feedback and verifier cost scheduling are not part of this path.

The NVIDIA scheduler uses vLLM 0.31's ordinary waiting and KV-holding waiting
queues. The pinned MetaX core uses its waiting and skipped-waiting queues. Workload
observations and schedulable-work checks read the corresponding native queues;
remote waits continue to use running requests with decode eligibility deferred.

## Frontend and role ownership

Managed role services run behind the existing Rust model-server admission owner.
It loads projected instance limits, accepts Target request reservations and holds
permits across the session stream. Python owns engine sessions and transport
artifacts; it does not duplicate the platform admission store or limit watcher.

Target owns a request session across rounds. Target prefill emits the first
confirmed token; the caller then forms each batch, selects DT, and executes:

1. Reuse the selected Draft binding with the confirmed delta, or bind the full
   confirmed prefix on a newly selected Draft.
2. Ask Draft for candidates within the remaining output budget.
3. Submit candidates using the Target's current ticket.
4. Return the actual Target delta, stop outcome and request state to the caller.

Each engine owns local batching and KV state. Draft rounds are ordinary vLLM
requests against the confirmed prefix, using native prefix caching where the method
permits it. Target owns verification and final token commitment. The normal frontend output processor
retains text decoding and stop-string handling; it does not reconstruct engine
token state by re-tokenizing cropped text.

The caller owns returned request state; dropping it closes both role sessions.
Dropping an executing round closes members still owned by that round. Draining rejects new
bindings while allowing existing rounds to finish. Engine health/readiness stays
available during drain so Service routing does not interrupt existing sessions.
Session close waits for native abort followed by an ordered worker utility that
synchronizes the MRV2 compute stream. The Rust ingress retains admission capacity
until that acknowledgement and artifact cleanup complete; enqueueing an abort is
not a termination acknowledgement.
Frontend or role failure terminates the request. Request state records the current
Target session/KV location across batches.
Ordinary rounds reuse that Target. Explicit migration snapshots its native pages
and loads them through the destination native block allocator before resuming;
transparent replay is not implemented. Multiple Draft replicas provide capacity, not collaborative
candidate trees or intermediate verification.

After admission drain, model-server signals the role launcher first. Its lifespan
closes Worker-owned transport registrations before stopping EngineCore. Sending
SIGTERM to the whole process group at that point would stop the Worker while
the role still needs cleanup RPCs. The existing process-group teardown remains
the fallback when the application does not exit within the drain deadline.

## Worker and transport boundary

`DraftTargetWorkerExtension` is installed through the native `worker_extension_cls`
entry point. It connects the plugin Runner's GPU I/O hooks to Mooncake; the
EngineCore and scheduler adapters contain no Mooncake-specific behavior.

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

## Observation ownership

`ObservedScheduler` wraps the native Draft scheduler; `ExternalScheduler` adds
Target remote-candidate admission. Native queues and page tables remain the only
load/KV authority. Schedule/output hooks accumulate wall durations and sampler
acceptance without copying the scheduling algorithm. The one-worker synchronous
execution constraint makes each scheduled step's completion unambiguous.

`ExternalAsyncLLM` consumes cumulative output observations through EngineCore's
utility queue, derives interval deltas, and deletes remaining records when its
generator closes. Native abort notifications have no model step to measure.
The role adds Worker-measured transfer timing; the frontend owns caller queue,
proposal-batch wait, round identity, tracing and aggregate metrics. See the
[measurement contract](role-protocol.md#observations) for boundaries and KV limits.

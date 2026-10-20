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
preparation supply the NVIDIA transport environment. The CUDA image builds the
pinned Mooncake engine with CUDA IPC and its public registration-refresh C API;
the upstream wheel does not export that C API. The
[selection backport](../../patches/mooncake/intra-nvlink-selection.patch) preserves
RDMA/TCP startup when only the intra-node NVLink backend is compiled.
Mooncake sources and added build dependencies stay in
the build stage. MetaX retains its own runtime build. The
[model-server image](../../model-server/Dockerfile) installs the DT
distribution and its `vllm.general_plugins` entry point in the engine's Python
environment. The launcher extends inherited plugin allowlists before spawning
EngineCore. Source delivery carries the same package; no runtime source mount is
required.

Ordinary Foretoken engine backports use the shared version-selected patch series.
DT does not maintain an additional engine image or ready-metadata patch.

## Engine boundary

The plugin configures `method="external"` and exposes
`ExternalAsyncLLM.submit_external_draft_batch([(ticket, token_ids, work_identity), ...])`.
All entries reach the scheduler in one utility call, returning one admission result
per entry; native scheduling still chooses physical GPU batches. A nonterminal
`RequestOutput.external_draft_request` carries an immutable request/generation
ticket after normal output stop handling. Each result reports whether its ticket
was admitted, not whether verification accepted the candidates. Stale or consumed
tickets return false; an empty candidate list requests a Target-only step.

| Owner | Responsibility |
| --- | --- |
| Scheduler | Wait per request, admit each ticket once, recover KV at the same frontier, account for verification work |
| EngineCore | Receive submissions through the existing utility queue; sleep when only external waiters remain |
| MRV2 | Resolve current request slots, fill GPU draft-token state before input preparation, consume staged GPU proposal distributions through native rejection sampling |
| Plugin AsyncLLM | Attach the next ticket after native output stop handling; never issue one on terminal output |
| Foretoken frontend / Router | Normalize input, choose a fixed Draft set and Target per request, retain routing reservations and process confirmed output |
| TargetCoordinator | Own one request’s confirmed prefix, round and propose/verify progression |
| RoleService | Own native sessions, independent proposal preparation, ticket admission and cleanup |

No local Draft model is constructed by the Target engine. Updating
Scheduler's CPU candidates alone is insufficient: MRV2 input preparation reads
`req_states.draft_tokens`, so the plugin fills that state after request-slot
updates. EngineCore distinguishes lifecycle ownership from schedulable work to
avoid stepping continuously while remote candidates are outstanding. Queued
requests count as schedulable only while native running admission has room,
including slots retained for streaming input. When every running request waits
for Draft and admission is full, EngineCore sleeps on its input queue; candidate
submission or cancellation wakes it. Finished-request cleanup and in-flight
batches continue through the normal engine loop.

The three strategy boundaries are the existing Router pipeline for request
placement, `TargetCoordinator.select_candidate_count()` for round K, and native
Scheduler subclasses for local token scheduling. Budget selection does not own
frontier or execution. `ObservedScheduler.schedule()` and
`ExternalScheduler.schedule()` are the local batching extension boundary; their
default continues to call native scheduling.

Target round policy and native capacity are separate. The ordinary Target uses
`--draft-tokens` for its default K and `--max-candidate-tokens` for native
`num_speculative_tokens`; the capacity defaults to K. Neural capacity remains in
its native speculative configuration. MRV2 retains startup-sized candidate and
sampling buffers; shorter submissions populate only their actual rows, and
`dynamic_sd_lookup` prevents synthetic candidate padding. Native scheduling
still selects the actual verification work.

Ordinary autoregressive Draft rounds use native generation `max_tokens` and have
no artificial static K limit. Neural Drafts compute their native block; the
adapter truncates returned tokens and exported q rows to the requested prefix.
Their native block length is the advertised method limit. Request-level routing
checks execution compatibility; each round caps the configured K by the selected
method and Target capacities. These fields are carried by role protocol v10 and
`DraftTargetCapabilities`; `requested_tokens` records the selected round count
alongside proposal, verification and acceptance counts.

Tickets pair vLLM's unique internal request ID with its cumulative confirmed output
token count. The client retains the internal ID from the native output collector;
no cross-process output schema is changed. Waiting requests remain in the native
running queue and remain eligible for KV preemption. Preemption retains the ticket,
its unconsumed/accepted state and any accepted candidate chain. KV recovery is a
physical replay of the confirmed prefix: it emits no token, keeps the same frontier
and leaves the confirmed anchor to be recomputed with the candidates. Scheduler
and MRV2 apply matching CPU/GPU computed-token rollback at the final replay step.
Replay can overlap remote preparation; ready candidates are admitted once before,
during or after replay and execute only after recovery. If candidates are still
outstanding, the recovered request sleeps until their submission.

The ticket needs no KV-residency epoch: synchronous EngineCore execution serializes
admission and preemption, while the waiting flag consumes a ticket exactly once.
The accepted chain survives native clearing of `spec_token_ids` and is populated
again only for verification. A new confirmed output consumes the round and issues
the next frontier; abort/completion removes the request, and a recreated session
has a new native request identity. Late or duplicate submissions cannot reuse it.

This mode supports text, one worker, eager execution, synchronous local scheduling
and output interval one. `draft_sample_method="probabilistic"` consumes external
proposal distributions; the token-only mode requires greedy sampling. RDMA Draft
also uses the plugin sampler's processed-logit export. Remote waits are per request;
other requests can still execute. Distributed model execution, CUDA graphs,
multimodal input and EC transfer are not enabled by this adapter. The optional
NVIDIA Target migration connector supports one full-attention KV group; other KV
connectors remain unsupported.

The role CLI preserves the native `VLLM_BATCH_INVARIANT` default and any explicit
environment setting. Comparison runs that enable it must use the same setting
for DT and Target-only execution. Native model/backend support and hardware
restrictions apply; independent Draft randomness prevents a general seeded-sequence
equality claim. See the [role startup requirements](../README.md#start-two-roles).

## Greedy validation

Compare complete token IDs with a native Target-only run using the same model,
tokenizer, dtype, decoding options, prefix-cache setting and batch-invariance mode.
At the first mismatch, retain the confirmed prefix, ticket, input IDs, positions,
computed-token frontier, attention metadata and leading logits. Trace the earliest
different layer, checking cached prefix KV before that query. Similar logits or a
successful request do not make a token-ID mismatch pass.

On the pinned MetaX BF16 Qwen3-4B path without batch invariance, a single-token
decode and a four-token verification query differed first at the native QKV GEMM,
despite identical first-row input and cached prefix KV. A later top-logit tie became
a strict ordering, changing the greedy token. Replaying native linear layers with
the same input row and weights reproduced batch-shape variation independently of
request coordinator, Draft execution and transport.

The MetaX source manifest applies `metax-batch-invariant-matmul.patch` to native
vLLM. Its default persistent GEMM tile requires 96 KiB shared memory; the tested
device provides 64 KiB. The patch selects smaller M/N tiles on devices below that
limit while retaining the K reduction tile and native execution. With
`VLLM_BATCH_INVARIANT=1`, isolated Target verification matched native Target-only
token IDs for all eight cases at budgets 1 and 3, including deliberately incorrect
candidates, correction, EOS and max-token termination. Native QKV and LM-head
single-row/four-row replays were also identical. This verifies that model/backend
combination, not all models or an end-to-end N:M/RDMA deployment, and does not
measure performance. The default remains disabled; changing the mode requires
re-running both sides of the comparison.

MetaX FlashAttention's fixed decode split count does not make chunked prefill
and decode use identical reductions. Qwen3-8B concurrent requests exposed logit
variation even with batch invariance enabled. The source manifest therefore
marks that backend as incompatible with batch invariance and registers the
matching core vLLM Triton backend. Its fixed-reduction attention is selected
through native capability filtering; the SDK's copied Triton backend still
imports an obsolete KV-layout API. No DT scheduler or sampler is replaced by
this adaptation. With batch invariance disabled, FlashAttention remains the
default. Strict comparisons must use the same attention backend on both sides.

The retained-state regression on MXC500 uses this source runtime, BF16 and eager
execution with Qwen3-8B Target, block-7 DSpark or ordinary Qwen3-4B Draft, K=3
and same-host ZMQ TCP. Both Draft profiles matched complete Target-only token IDs
and finish/stop reasons for serial, eight-concurrent and twelve-request KV-pressure
workloads. Explicit Draft/Target KV eviction, duplicate-ticket rejection and
proposal-preparation cancellation also passed; final session, artifact, transfer
and native KV counters returned to zero. Cancellation during a measured in-flight
transfer and random-sampling distributions were not covered by this regression.

Earlier end-to-end greedy checks also exercised Qwen3-8B with a block-7 DSpark Draft and
Target K=3: native vLLM 0.31.0 on A100 over cross-host ZMQ TCP, and MetaX's pinned
0.30 runtime on MXC500 over same-host ZMQ TCP and MACA GPU IPC. Both sides used
BF16 and batch invariance. Complete token IDs and finish/stop reasons matched
Target-only for single and eight-concurrent request workloads, including natural
rejection, EOS and shortened final budgets. Cancellation drained sessions and
artifacts; native records contained multiple-request physical batches. This
validates those execution profiles, not random-sampling distributions, NVIDIA
GPU IPC, RDMA, hardware link utilization or a full Kubernetes rollout.

## Neural Draft context

Neural profiles use the pinned native speculator on NVIDIA or MetaX. Same-host
ZMQ TCP/IPC stages features and proposal distributions through host memory; RDMA uses
Mooncake. Roles advertise initialized `tensor_transports`; Router requires a
compatible path, and Target selects eligible same-host GPU IPC first, then RDMA
and ZMQ, once per request. GPU IPC requires mutual granted peer access metadata.
The default launcher offers TCP, allocated RDMA and supported CUDA/MACA peers; explicit ZMQ IPC requires shared
same-host paths. RDMA startup also checks actual GPU registration. Before prefill, the selected
pair shares a connection-owned proof of both GPU read directions across requests;
uncertain transfers retain the usual drain/process-exit boundary.
Pools are keyed by backend as well as tensor geometry/dtype. These paths preserve
request-level routing or native token scheduling. ZMQ send uses copied frames, and
source snapshots remain owned until ACK or unclaimed revoke; receiver H2D must finish
before admission. GPU-consumption fences still govern destination buffer reuse.

With a neural Draft profile, Target retains a position-indexed history of final and
auxiliary hidden states through a non-model speculator. Export runs on the
EngineCore scheduler thread and selects only the confirmed computed prefix,
excluding the last correction/bonus token. Rejected suffixes are discarded;
request completion and abort release the retained history. Native slot removal
for preemption retains that history and the Target's staged proposal binding.
Replay does not overwrite the logical features. An export made before or during
recovery therefore describes the same prefix; its independently owned buffer stays
valid while Draft computes or transfers candidates. Proposal distributions remain
with the session until verification consumes them or cancellation joins cleanup.
KV eviction never revokes a publication or proves a transfer complete; the normal
claim/read/ACK and GPU-consumption fences still govern buffer reuse.
Target APC is disabled so a cache hit cannot leave gaps in that history.

TargetCoordinator supplies the bound Target session address during Draft preparation.
Draft requests only the missing interval `[start_token, end_token)` from that
session's confirmed feature prefix, excluding the final anchor. Profile, full
confirmed tokens, interval and tensor layout are checked before consumption.
The first interval starts at zero. Draft advances its retained-prefix cursor only
after a completed read is appended to its Worker-owned `DraftContext`.
Claim/read/ACK and the transfer buffer's last-use fence remain with the existing
artifact owner. Session features survive individual transfers and KV eviction;
closing the session joins native abort and outstanding operations before dropping them.

`DraftScheduler` uses native streaming input to retain one native request and its
private pages across turns. The input for reconciliation is the complete confirmed
prefix; the computed-token cursor advances only through its still-valid rows.
For an ordinary autoregressive Draft this includes matching computed candidates.
For neural methods reuse stops at the previous confirmed feature frontier, and
the shifted token/anchor input must also match. Proposal-conditioned tails are
recomputed from newly confirmed Target features. Draft APC is disabled so no
mutable tail can be shared or published under a token-only cache key. Full-attention
KV is required; recurrent-state and sliding-window Drafts are rejected at startup.
No Target page index crosses the role boundary.

Resumed neural turns disable native static speculative padding. A turn with just
one new confirmed feature must not be padded with synthetic positions lacking
Target features. The native method still produces its configured proposal block;
the adapter returns only the requested K candidates and distribution rows.

A completed Draft turn remains a sleeping, preemptible native resident. Native
allocation can evict its KV; an evicted idle turn owns no runner slot and does not
replay its already returned candidates. The next confirmed input makes it ready
again, using retained features for any necessary replay. EngineCore sleeps when
only parked turns remain. Abort joins the native streaming-input task and its
pending admission before fencing GPU work. The native output processor gets a
fresh per-turn view so it does not append complete prompts or rejected candidates
to an ever-growing output history.

The neural loader initializes the Target on meta and materializes shared
embedding/output and normalization weights, preserving tied parameters. MRV2 loads
the selected native speculator and allocates KV. Feature forward reads only the
locally scheduled rows of the retained prefix, and sampling supplies the confirmed
anchor. Native execution, batching and request postprocessing remain in the engine.
The adapter returns the native proposal at the end of each input turn; partial
prefill chunks return no candidates. Profiling and warmup use synthetic features
without remote bindings.

The adapter enables DFlashDraftModel, Qwen3DSparkModel, EAGLE/EAGLE3 and
single-module MTP. Multi-module MTP, HC residuals and Target-shared sparse indices
remain excluded. Gemma4 needs Target KV sharing. Other DFlash and DSpark
architectures require separate integration. DSpark retains native anchor layout,
Markov sampling and reduced-vocabulary mapping. Export reads native `draft_logits`
after Markov bias and any `dspark_draft_topk` truncation, preserving each q row's
conditioning on preceding sampled candidates. Remote roles reject
`enable_adaptive_verification`; confidence scheduling is not part of this path.

The NVIDIA scheduler uses vLLM 0.31's ordinary waiting and KV-holding waiting
queues; the pinned MetaX core uses waiting and skipped-waiting queues. Workload
and readiness observations use the corresponding native queues.

## KV recovery and CPU offload

The DT adapter currently uses native KV recomputation after eviction. Its optional
KV connector is the explicit checkpoint/resume adapter; CPU KV offload and LMCache
are not integrated. `cpu_offload_gb` offloads model weights, not request KV.

Resident Draft turns resume in native `running`, where allocation failure can
trigger native victim selection and KV reclamation. Refreshing a turn's prompt,
sampler and feature binding does not require waiting-queue admission:
`DraftScheduler._make_cached_request_data()` excludes refreshed turns from
incremental slot updates, and `_update_after_schedule()` supplies native
`NewRequestData` before advancing computed-token counters. MRV2 rebuilds those
slots using the retained block table. A turn preempted before its next schedule
instead follows ordinary waiting admission and native slot reconstruction.
The refresh marker survives a token-budget deferral or preemption until the turn
is actually scheduled; it neither owns KV nor changes the confirmed frontier.

Native FCFS/priority scheduling still owns admission and victim selection;
preserving candidates does not itself provide a starvation guarantee
under an unbounded workload or a KV configuration unable to run one request.

## Frontend and role ownership

Managed role services run behind the existing Rust model-server admission owner.
It loads projected instance limits, accepts Target request reservations and holds
permits across the session stream. Python owns engine sessions and transport
artifacts; it does not duplicate the platform admission store or limit watcher.

The selected Target owns peer HTTP and transfer operations through `RoleCommunication`;
`DraftPeerSession` retains each control stream while `TargetCoordinator` decides request rounds.
Native prefill emits the first confirmed output and ticket. The coordinator commits
exact deltas, obtains one chain and calls local verification. Each request prepares
and admits independently; native scheduling combines compatible ready requests.
The Target default candidate count is 3, capped by remaining output; its engine
capacity can be configured independently. There is
no shared stage pool, global rules policy or explicit logical-batch scheduler.

Each engine owns local batching and KV state. Draft rounds are turns of one
retained native streaming-input request, reconciled against Target confirmation. Frontend's
normal output processor retains text decoding and stop-string handling. It does
not relay per-round candidates or reconstruct state from display text.

Dropping the client stream explicitly closes the Target session and its bound Draft. Draining
rejects new bindings while allowing existing rounds to finish. Engine health and
readiness remain available so Service routing preserves active sessions. Session
close waits for native abort and an ordered Worker utility that synchronizes the
MRV2 compute stream. The ingress retains admission capacity until termination
acknowledgement and artifact cleanup complete.

Transport or engine failure terminates the request without replay. Existing
checkpoint/resume remains a manual, unbound-session diagnostic with its model,
sampling and KV-layout restrictions. Automatic migration is outside the current
request-binding path. Binding is fixed by this delivery, not an engine-neutral
requirement that all future Target placement remain resident.

After admission drain, model-server signals the role launcher first. Its lifespan
closes Worker-owned transport registrations before stopping EngineCore. Sending
SIGTERM to the whole process group at that point would stop the Worker while
the role still needs cleanup RPCs. The existing process-group teardown remains
the fallback when the application does not exit within the drain deadline.

## Worker and transport boundary

`DraftTargetWorkerExtension` is installed through the native `worker_extension_cls`
entry point. It connects the plugin Runner's GPU I/O hooks to the selected tensor
transport; EngineCore and scheduler adapters contain no transport-specific behavior.

Draft marks each turn with `SamplingParams.extra_args["dt_artifact_id"]`.
The sampler observer captures processed logits returned by native `Sampler.sample`,
the shared entry point in both pinned runtimes. RoleService supplies independent
per-proposal seeds, keeping native sampling on its materialized distribution path.
The Worker captures complete float32
`log(q)` rows without copying probabilities to the API process. Worker RPCs
publish source buffers, start/poll destination reads, stage received artifacts
against engine tickets, and return buffers to an exact-shape pool after their
owners finish using them. Device buffers remain reusable until role shutdown;
RDMA registrations stay with those buffers. ZMQ host snapshots are released on ACK
or unclaimed revoke. Idle device buffers are excluded from retained-artifact counts.

Target's role waits for read readiness, ACKs the source, stages the tensor and
submits candidates through the existing EngineCore utility queue. MRV2 maps the
staged distribution to current GPU request slots before native rejection sampling.
Submission success is not consumption completion: the role retains Target storage
until subsequent engine output or abort. See the
[Connector contract](connector-contract.md) for scaling and temperature semantics.

`TensorTransport` owns reusable buffers; `MooncakeTransport` adds device registrations,
`GpuIpcTransport` reuses Mooncake CUDA/MACA handles/copies, and `ZmqTransport` owns
sockets and immutable host snapshots. `TensorBuffer` fences producer writes
and the final local consumer with CUDA events. RDMA completion is independent of
CUDA producer readiness. A completed-read ACK permits source release, not reuse
of a destination still in use by model execution. Uncertain buffers remain
retained until process termination. The standalone diagnostic exercises the
transport separately and does not establish inference correctness or performance.

## Observation ownership

`ObservedScheduler` wraps the native Draft scheduler; `ExternalScheduler` adds
Target remote-candidate admission. Native queues and page tables remain the only
load/KV authority. Schedule/output hooks attach stable physical batch and work identities, and accumulate wall durations and sampler
acceptance without copying the scheduling algorithm. The one-worker synchronous
execution constraint makes each scheduled step's completion unambiguous.

`ExternalAsyncLLM` consumes cumulative output observations through EngineCore's
utility queue, derives interval deltas, and deletes remaining records when its
generator closes. Native abort notifications have no model step to measure.
The role adds Worker-measured transfer timing; TargetCoordinator owns round identity and remote-stage wall durations; Frontend records
received observations in tracing and aggregate metrics. See the
[measurement contract](role-protocol.md#observations) for boundaries and KV limits.

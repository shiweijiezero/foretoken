<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# DT Connector: proposal distributions and ownership

English | [简体中文](connector-contract_zh.md)

The Connector connects independent Draft and Target workers. HTTP carries
request control, candidate IDs and immutable tensor descriptors. Mooncake reads
device tensors through RDMA; ZMQ TCP/IPC stages them through host memory. CUDA/MACA GPU IPC reuses Mooncake
for same-host device copies between mutually granted peers. The
[role protocol](role-protocol.md) defines the control endpoints; the
[engine contract](mrv2-integration.md) defines MRV2 integration.

## Transport selection ownership

Router selects the request's Draft and Target. Target's `RoleCommunication`
matches their initialized adapters at binding: eligible same-host `gpu_ipc`,
then `rdma`, then `zmq`. The GPU path also requires mutual granted peer access.
This choice fixes the adapter used by that request's artifacts.

The Mooncake adapter owns registration and completion through the existing buffer
lifecycle. Mooncake owns native transfer submission, topology-aware NIC selection
and internal path handling. CUDA/MACA owns the physical GPU peer path. `gpu_ipc`
means IPC-based device copies; it does not identify NVLink, MetaXLink or PCIe.
ZMQ is a separate host-staged adapter. HTTP control keeps its existing owner.

## What crosses the boundary

| Direction | Payload | Producer and consumer |
| --- | --- | --- |
| Target → Draft | Confirmed prefix, version, sampling settings, candidate budget | TargetCoordinator → Draft role |
| Draft → Target | Candidate IDs, version and artifact ID | Draft role → Target role |
| Target → Draft → Target | Claim artifact ID, receive `PayloadRef` | Target role → Draft Worker → Target role |
| Draft GPU → Target GPU | Contiguous float32 `log(q)` with shape `[candidate_count, vocab_size]` | Draft sampler/Worker → Target Worker/native rejection sampler |
| Target GPU → Draft GPU | Confirmed computed features `[tokens, feature_layers, hidden_size]` | Target feature collector → native neural Draft |
| Target → Draft | Completed-read ACK identifying the source publication | Target role → Draft release endpoint |
| Target → Draft | Exact confirmed token delta, next version or termination | Target output processing → Draft confirmed-prefix state |

Each distribution row describes the actual draw for its candidate position after
Draft temperature and supported truncation. It is not the probability of only
the chosen token, nor unprocessed model logits. A greedy proposal uses log
probability zero for its chosen token and negative infinity elsewhere.
Both models must share vocabulary size and token-ID meanings.

`PayloadRef` carries `publication_id`, `transport`, `segment`, `address`, `nbytes`,
`dtype` and `shape`. RDMA and GPU IPC use a Mooncake segment and positive device address;
ZMQ uses a TCP or IPC endpoint and null address, pulling by immutable publication ID.
TCP endpoints include the port bound by the Worker; IPC needs shared same-host paths.
The descriptor identifies storage, not an inference acceptance decision.
Target allocates its own destination device storage and checks layout. The
frontend never reads tensor contents or serializes probabilities as JSON.

Features are transferred as contiguous confirmed intervals. The Draft session
retains the received prefix; after its first read, only newly confirmed rows cross
the link. Received transfer buffers are fenced and recycled after append, while
session features remain until native abort and operation cleanup complete.

## Peer control and transfer ownership

Each role has one `RoleCommunication` owner and HTTP connection pool, shared by
control messages, feature export, tensor claim/read/ACK and RDMA proofs.
`DraftPeerSession` retains each request's remote session, open NDJSON stream and
single close task. Closing that session releases its stream and remote work;
it does not close the shared client or another request's connection.

`TargetCoordinator` decides when to propose, commit, revoke and verify, and owns
confirmed progress. It does not construct URLs or manage HTTP resources.
`RoleService` validates model context, tensor layout and engine tickets before
staging or admission. Communication retains started read/ACK operations independently
of their waiters. Read readiness hands local consumption to the caller; ACK and
source reclamation continue under the same transfer owner. The request resumes
after feature append or ticket admission; its session retains the operation until
ACK and destination reclamation complete. Completed cleanup failures are checked
before further rounds and joined at close. A neural Draft runs while its feature
transfer buffer is reclaimed; Target output also need not wait for its previous
proposal buffer's reclamation. Destination storage remains owned until local use
is fenced even if a later ACK fails.
Worker transports continue to own all physical buffers and completion fences.

For routed neural requests, Target starts feature preparation on confirmed output.
Its existing snapshot/publication operation reserves the single reader. The next
proposal carries interval metadata and an artifact handle; the physical descriptor
stays in the source session inventory until Draft claims it. The claim endpoint
hands off that prepared result without another Worker/Core call. A diagnostic
`/context` export still hands the descriptor directly to its caller.

Source close joins preparation. It can release an unclaimed prepared result using
the exact publication ID, since no remote reader received its descriptor. Claim
reserves remote ownership before yielding; after that, close waits for the ordinary
reader ACK. Lost claim/export responses retain uncertain ownership. After the first
Draft turn, read readiness also queues feature append at the Core input boundary.
Core processes native aborts and validates the retained turn before append; I/O
threads only notify. Completed reads keep their ACK proof even when append is
rejected or fails. Uncertain utility results or failed appends retain destination
ownership until abort/fencing or process exit.

At role shutdown, request sessions and shared communication operations are joined
while the HTTP pool remains open for close messages and ACKs. Existing Worker
drain follows; the role lifespan closes the pool before shutting down the engine.
A failed or uncertain transfer keeps the existing quarantine/process-exit boundary.
The pool adds no request capacity limit: existing admission remains the owner,
and active session streams cannot exhaust a separate connection quota.

Preparation telemetry reuses these same operations. Source claim/ACK replies
carry measured Worker RPC phase totals; cached feature claims have no Worker RPC.
The receiver combines those observations with completion wait,
allocation, prior-use fencing and backend-read wall times. Feature export, append
and receiver release remain distinct. Coordinated export measures source-side
preparation; manual export measures the HTTP round trip. Feature totals sum source
and receiver intervals, excluding the intervening control handoff. These nested durations are retained in the
round's transfer breakdown and do not change ownership or add RPCs/fences. See
[observation boundaries](role-protocol.md#observations).

## Worker and engine responsibilities

1. Draft's MRV2 sampler exposes processed logits. The worker extension captures
   full `log(q)` rows on GPU and publishes them after the producer CUDA event.
2. Target claims the artifact directly from Draft to obtain its descriptor,
   then awaits a native utility Future for the selected transport. Completion
   notification does not occupy the model execution loop or require repeated Worker RPCs. Transfer work
   runs outside model execution; the affected request waits without scheduling
   verification before its data is ready.
3. Tensor verification starts one native read/admission utility. Read completion
   queues metadata and wakes EngineCore, which drains native input and aborts,
   checks each ticket and binds its destination before admission. Slow reads do
   not hold ready members behind a barrier. After the utility reply the role
   starts source ACK independently of model execution. Token-only submissions
   retain client event-loop coalescing. Neither path adds a batching timer;
   native scheduling determines which requests execute together.
4. Scheduler forms local batches. Runner resolves current request slots and
   candidate positions, so transfer arrival order cannot define batch row order.
5. The adapter supplies `log(q) * target_temperature` to the native rejection
   sampler, which applies its own temperature division. Greedy rows use a unit
   scale. This preserves the meaning of the already normalized proposal data.
6. Target retains the artifact through the next committed output or request
   abort, then hands it to session-owned reclamation without delaying the next
   round. Releasing the source after DMA is independent of releasing the
   destination after model consumption. Pending ACK/release timings are null in
   an observation emitted before that operation completes.

The Target still owns verification, sampling and stopping. A successful
submission means admission to verification; only Target output commits tokens.
The next proposal carries the exact confirmed delta, applied atomically by Draft
before it prepares that proposal. A terminal output closes the existing pair
without another prefix synchronization. Manual commit remains available.
Draft randomness is independent of Target's RNG. The user seed controls Target
sampling and does not promise output equality across proposal schedules.

## Memory, cancellation and drain

Only a successful claim exposes a source descriptor. Draft Worker serializes
claim with cancellation: cancellation revokes an unclaimed artifact and returns
its buffer to the pool after local GPU work completes; a late claim is rejected.
Once claimed, source storage remains registered and immutable until its exact
publication is acknowledged. A cancelled HTTP request does not cancel DMA.
The Target claim/read/ACK task therefore survives its control caller's cancellation;
stale tickets prevent later inference consumption, not required transfer cleanup.
If Target abandons the artifact before starting any read, it ACKs without DMA.
Successful release of a claimed source waits for ACK and the last local GPU use,
then returns the buffer to a worker-owned pool keyed by transport, capacity and dtype.
Feature prefixes use geometric row capacities and publish only populated rows;
receivers may read those rows into a larger registered buffer. Other payloads use
exact shapes. This avoids retaining a different full feature buffer for every
prefix length.
Registration persists across rounds; the pool retains peak concurrent buffers
for each shape until role shutdown. Idle buffers do not count as retained
artifacts. The worker retains
uncertain transfers and claimed, unacknowledged publications rather than reusing memory.
Those resources may require process termination if a peer fails.

`/status.retained_artifacts` reports transport-owned artifacts separately from
`active_sessions`. `/status.transport` also reports registered buffer count/bytes,
idle buffers, pending reads and unacknowledged publications for sustained-run
ownership diagnosis. Bytes measure retained tensor capacity, not unique allocator
bytes. Reusable idle registrations are distinct from live artifacts.
Controller telemetry `running_requests` remains a session
count. The model-server supervisor waits for both counts to reach zero within
the existing shutdown deadline. It does not claim transparent migration or replay.

## Supported scope and remaining methods

Tensor roles advertise `token_ids_log_probs` and their initialized `tensor_transports`.
Router requires a compatible capability intersection. GPU IPC additionally
requires matching host identity and mutual `gpu_ipc.peer_devices` access. Target
selects eligible GPU IPC first, then RDMA, then ZMQ, and passes the path to the Draft session and Target feature exports. Each
artifact retains its original backend through publication, read and release.
Auto offers ZMQ TCP, any explicitly allocated RDMA connector and CUDA/MACA GPU IPC
when granted device access and the native peer matrix allow it; ZMQ IPC remains an
explicit shared-directory override. Auto logs missing Mooncake or RDMA initialization
failure and omits RDMA before model transfers. RDMA startup requires an HCA and
successful registration on the actual Worker GPU; native TCP/MACA/NVLink overrides
are scoped out during RDMA initialization and restored afterward. Forced RDMA fails
startup if those checks fail. Programming errors and failures after transfer starts are not downgraded. Binding selection is logged with request/endpoints.
Roles without tensor transport advertise `greedy_token_ids` and accept temperature zero only. Router checks Target availability using its referenced Draft pools and matching
format, a common transport and context profiles; both members must also be in the caller's allowed pools,
excluding token-only roles for random sampling. Target
checks the routing-assigned Draft's live capabilities before execution. A selected pair must agree;
there is no automatic reduction of a probability-bearing request to token-only
execution. Each role currently uses one GPU worker, the native `uni` executor and local
synchronous scheduling. Eager is the default; native CUDA Graphs are optional.

Explicit Target migration reuses the artifact claim/read/ACK lifecycle for a uint8
KV checkpoint. Both Targets must enable migration and use compatible model and
layer layouts. Native pages are copied at the source; the destination loads its
received copy into pages allocated by its own scheduler. See the
[checkpoint protocol](role-protocol.md#target-kv-checkpoint-and-resume).

DFlash, Qwen3 DSpark, EAGLE/EAGLE3 and single-module MTP use Target feature export and native
Draft consumers, with BF16/FP16/FP32
features carried by the same artifact lifecycle. The frontend checks matching
context profiles before admission. Other variants remain outside the enabled
[neural Draft scope](../README.md#run-a-neural-draft-pair).
Trees need branch structure, tree attention and accepted-path cleanup. Cascaded
verification needs stage dependencies and intermediate verifier semantics. KV
sharing and offload need their own placement and ownership paths; they are not
implemented by the checkpoint operation or by adding a tensor descriptor.

Cross-host stochastic correctness, cancellation and resource-release validation
must exercise the actual model chain. Performance and complete Kubernetes
lifecycle acceptance are separate from the standalone tensor diagnostic.

## Same-host GPU IPC

`GpuIpcTransport` uses Mooncake's `nvlink_intra` backend on NVIDIA and `maca`
on MetaX. It queries peer access in a separate metadata process because the
runtimes cache device visibility. Device permission checks restrict discovery to
the GPUs granted to the container; the compute Worker keeps its binding.
Capabilities report host boot identity `host_id`, opaque `device_id` and
`peer_devices`. Discovery and binding require the same host and mutual access.
This does not modify Kubernetes GPU allocations or device permissions; ordinary
single-GPU Pod isolation normally retains RDMA/ZMQ.

Startup checks the actual allocator's IPC export before advertising this path.
CUDA uses native IPC-compatible allocations; an allocator that cannot export them
is unavailable. Each underlying allocation is registered once and unregistered
only after its last retained Torch suballocation closes. Backend-selection
settings are scoped to engine initialization and restored afterward. Producer
events, claim/read/ACK, backend-specific pools and consumer fences retain their
existing owner. Published descriptors never switch backend.

Before submission, all Mooncake adapters use the public `openSegmentNoCache` C API
with the Python engine's native pointer to refresh the peer's registrations,
including allocations published after the first open. Refresh and submission
serialize per peer; completion polling and other peers remain concurrent.
Both normal CUDA and MetaX builds retain this C API. The CUDA build also enables
`USE_INTRA_NVLINK`; its selection backport preserves ordinary RDMA/TCP operation.

Only probabilities/features use this selection. Manual KV checkpoints retain
strict NVIDIA RDMA startup and their existing restrictions. Peer import/copy and
cleanup need validation under the actual container configuration. Shared IPC
namespace alone does not establish device access. The `gpu_ipc` observation
identifies the adapter, not proof of NVLink/MetaXLink utilization; physical link
counters and performance require separate measurement.

## RDMA binding proof

Local initialization and GPU registration do not prove peer reachability. Before
Target prefill, the communication layer establishes the selected GPU pair with
two probe publications, one in each direction. `/transport/rdma/probe` and
`/transport/rdma/probe/read` use the existing Worker artifact claim/read/ACK path,
validate GPU contents and release completed storage. Requests share one pending
proof and reuse its successful result. They do not own probe resources; cancelling
a waiter closes only that request's sessions.
If verification fails, the connection revokes unclaimed probes and joins their
local fences. A claimed probe retains its owner until ACK or process exit.

`/status.rdma` identifies the actual Worker Mooncake engine incarnation and its
segment. The identity is generated when that engine initializes, fixing its GPU,
NIC configuration and backend. A restart/rebuild changes it even if the HTTP URL
is unchanged. Both peers check expected identities before publishing or reading;
Draft admission also returns its identity to close the status/open race. The
communication owner retires replaced or failed peers through one retained close
task. It joins started operations and revokes unclaimed probes; claimed publications
keep their ACK ownership. Retirement never cancels an in-flight operation.
No TTL, device scan or per-request speed test is used.

The pinned Mooncake Python adapter exposes segment handles and completion polling,
not peer-reconnect notifications. Registration refresh with `openSegmentNoCache`
is not a new transport incarnation. A failed RDMA read invalidates that peer's
proof. The existing live `/status` binding check also reports failed peer identities,
so a failure observed on the Draft receiver invalidates the Target's cached proof.
Subsequent requests fail until transport re-establishment, with no automatic
retry, fallback or transparent recovery. In-flight operations retain their original
owner. Role shutdown joins shared operations before Worker transport drain.
Failed or uncertain reads remain registered until completion is proven or the
owning process exits; disconnect alone never authorizes buffer reuse.

Discovery's `rdma` path means local HCA discovery and actual Worker GPU registration
succeeded. `/status.rdma_connections` reports peer count, initiated bidirectional
verifications and pending communication operations; the `DT RDMA peer verified`
log records both actual directions. These checks establish contents and ownership,
not GPUDirect bandwidth or performance.

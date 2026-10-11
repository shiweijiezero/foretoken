<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# DT Connector: proposal distributions and ownership

English | [简体中文](connector-contract_zh.md)

The Connector connects independent Draft and Target workers. HTTP carries
request control, candidate IDs and immutable tensor descriptors. Mooncake reads
the Draft's proposal distributions directly into Target GPU storage. The
[role protocol](role-protocol.md) defines the control endpoints; the
[engine contract](mrv2-integration.md) defines MRV2 integration.

## What crosses the boundary

| Direction | Payload | Producer and consumer |
| --- | --- | --- |
| Frontend → Draft | Confirmed prefix, version, sampling settings, candidate budget | Frontend round coordinator → Draft role |
| Draft → frontend → Target | Candidate IDs, version and artifact ID | Draft role → Target role |
| Target → Draft → Target | Claim artifact ID, receive `PayloadRef` | Target role → Draft Worker → Target role |
| Draft GPU → Target GPU | Contiguous float32 `log(q)` with shape `[candidate_count, vocab_size]` | Draft sampler/Worker → Target Worker/native rejection sampler |
| Target → Draft | Completed-read ACK identifying the source publication | Target role → Draft release endpoint |
| Target → frontend → Draft | Exact confirmed token delta, next version or termination | Target output processing → Draft confirmed-prefix state |

Each distribution row describes the actual draw for its candidate position after
Draft temperature and supported truncation. It is not the probability of only
the chosen token, nor unprocessed model logits. A greedy proposal uses log
probability zero for its chosen token and negative infinity elsewhere.
Both models must share vocabulary size and token-ID meanings.

`PayloadRef` carries `publication_id`, `segment`, `address`, `nbytes`, `dtype` and
`shape`. The descriptor identifies storage, not an inference acceptance decision.
Target allocates its own destination device storage and checks layout. The
frontend never reads tensor contents or serializes probabilities as JSON.

## Worker and engine responsibilities

1. Draft's MRV2 sampler exposes processed logits. The worker extension captures
   full `log(q)` rows on GPU and publishes them after the producer CUDA event.
2. Target claims the artifact directly from Draft to obtain its descriptor,
   then starts and polls the Mooncake read through worker RPCs. Transfer work
   runs outside model execution; the affected request waits without scheduling
   verification before its data is ready.
3. After read completion, Target ACKs the source publication and binds the
   destination artifact to the engine request ID and generation. Only then does
   the role call `submit_external_draft_batch`.
4. Scheduler forms local batches. Runner resolves current request slots and
   candidate positions, so transfer arrival order cannot define batch row order.
5. The adapter supplies `log(q) * target_temperature` to the native rejection
   sampler, which applies its own temperature division. Greedy rows use a unit
   scale. This preserves the meaning of the already normalized proposal data.
6. Target retains the artifact through the next committed output or request
   abort. Releasing the source after DMA is independent of releasing the
   destination after model consumption.

The Target still owns verification, sampling and stopping. A successful
submission means admission to verification; only Target output commits tokens.
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
then returns the buffer to a worker-owned pool keyed by capacity and dtype.
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
`active_sessions`. Controller telemetry `running_requests` remains a session
count. The model-server supervisor waits for both counts to reach zero within
the existing shutdown deadline. It does not claim transparent migration or replay.

## Supported scope and remaining methods

RDMA roles advertise `token_ids_log_probs`. Roles without RDMA advertise
`greedy_token_ids` and accept temperature zero only. Router matches healthy peers
by scope and format, excluding token-only roles for random sampling. The workflow
rechecks live status before admission. A selected pair must agree;
there is no automatic reduction of a probability-bearing request to token-only
execution. Each role currently uses one GPU worker, eager execution and local
synchronous scheduling.

Explicit Target migration reuses the artifact claim/read/ACK lifecycle for a uint8
KV checkpoint. Both Targets must enable migration and use compatible model and
layer layouts. Native pages are copied at the source; the destination loads its
received copy into pages allocated by its own scheduler. See the
[checkpoint protocol](role-protocol.md#target-kv-checkpoint-and-resume).

DFlash, Qwen3 DSpark, EAGLE/EAGLE3 and single-module MTP use Target feature export and native
Draft consumers, with BF16/FP16/FP32
features carried by the same artifact lifecycle. The frontend checks matching
context profiles before admission. Other variants remain outside the enabled
[neural Draft scope](../README.md#run-a-neural-draft-pair-on-nvidia).
Trees need branch structure, tree attention and accepted-path cleanup. Cascaded
verification needs stage dependencies and intermediate verifier semantics. KV
sharing and offload need their own placement and ownership paths; they are not
implemented by the checkpoint operation or by adding a tensor descriptor.

Cross-host stochastic correctness, cancellation and resource-release validation
must exercise the actual model chain. Performance and complete Kubernetes
lifecycle acceptance are separate from the standalone tensor diagnostic.

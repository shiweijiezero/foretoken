<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# DT Connector: method payloads and engine boundaries

English | [简体中文](connector-contract_zh.md)

This review proposal defines cross-role data, its producers and consumers, and
required engine interfaces. The executable API remains the
[greedy role protocol](role-protocol.md). HTTP greedy inference and a separate
RDMA diagnostic do not establish a general DT Connector implementation.

## Ownership and method requirements

Frontend selects roles, orders stages and owns request lifetimes. Connector moves
control messages and data. Each engine owns execution, batching and KV. Only the
final Target commits tokens; transport completion never means candidate acceptance.
Control flows through frontend coordination; large tensors move directly between
roles through Mooncake, with only descriptors passing through the frontend.

| Method | Target → Draft | Draft → verifier | Missing integration |
| --- | --- | --- | --- |
| Independent greedy models | Confirmed context, round, budget, termination | Candidate tokens | HTTP works; no RDMA inference payload |
| Independent stochastic proposals | Context and sampling contract | Candidates and actual proposal distribution q, aligned by position and vocabulary | Draft q export, Target consumption, Worker tensor lifecycle |
| Feature-driven Draft | Method-specific hidden states/features, token and position alignment | Candidates and method-specific verification data | Target feature export, Draft feature input; matched model pair |
| Tree/multiple branches | Confirmed base context | Tokens, parents, positions, required branch probabilities | Aggregation, tree attention, verification and selected-path cleanup |
| Cascaded Draft/intermediate verifier | Prior-stage artifact and context dependency | Another candidate artifact | Stage orchestration and verification semantics; only final Target commits |

The distribution q must describe actual proposal sampling, including temperature,
truncation and penalties. A scalar probability for each selected token generally
cannot support rejection correction. Raw logits and normalized probabilities need
an explicit verifier contract. Deterministic proposals can use a point-mass
proposal distribution; payload requirements do not follow Target temperature alone.

Independent models usually own different KV. Copying it requires a separate
model/layer, layout, position and ownership contract for sharing, offload or
migration. Multimodal context delivery to Draft is also method-specific; it cannot
universally be dismissed as a prefill-only concern.

## Proposed control and data contract

Preserve Open, Propose, Verify, Commit and Cancel responsibilities. Use explicit
method-tagged artifacts, not arbitrary dictionaries or independent optional tensor
fields. The following objects are proposed semantics, not enabled API fields:

| Object | Required information | Authority |
| --- | --- | --- |
| Session | Model pair, token semantics, method/version, sampling contract | Frontend binding and engine capability validation |
| Task | Request/session, stage, round, base context version, artifact identity, budget | Frontend dependencies; Target verification ticket |
| Candidate artifact | Method, chain/tree structure, method-required data references | Producing stage, without advancing committed context |
| Context artifact | Token/position span, feature layers and semantics, data references | Target or designated upstream stage |
| Tensor reference | Publication ID, segment, address, bytes, dtype, shape | Producing Worker/transport owner |
| Commit | Exact token delta, termination; selected path for trees | Final Target |
| Transfer completion | Read-complete acknowledgement for one publication | Receiver transport owner, separate from acceptance |

`PayloadRef` now checks dtype, shape and byte-size consistency for nonempty
contiguous tensors. Destination layout must match exactly. Artifact semantics,
positions and vocabulary mappings cannot be inferred from shape. The consumer
chooses its local device, stream and address; a remote device ordinal does not
control allocation.

Advertise a method only when export, transport, import, verification and cleanup
all support it. Currently only `greedy_token_ids` is available. Future protocol
versions/methods must be rejected consistently by both roles and frontend when
unsupported, never silently reduced to token-only execution.

## Engine integration and batching

| Stage | Reuse or required extension |
| --- | --- |
| Export | Worker/Runner supplies real q/features and layout; publish after a recorded completion event. Public top-logprobs cannot reconstruct full q |
| Transfer | Connector allocates/registers local storage, finishes RDMA and ACKs source reads outside the compute thread |
| Readiness | Worker finishes device materialization and notifies Scheduler through EngineCore; descriptor arrival is insufficient |
| Batching | Scheduler builds verification batches; Runner resolves current slots and position views, never using arrival order as batch row identity |
| Verify | Reuse the applicable MRV2 sampler; external-token admission still needs a device-input lifecycle for probabilities/features |
| Commit | Publish after existing output stop handling; frontend schedules dependent work |

Remote waits are per request. This differs from vLLM asynchronous local scheduling.
Multi-GPU execution additionally requires shard/rank ownership, absent from the
current single-Worker mode. Batched transfers must preserve per-artifact identity.

## Memory, failure and scaling

1. Finish producer writes before publication; keep exported storage registered and immutable.
2. Fence previous destination consumers before reading; consume and ACK only after read completion.
3. ACK permits source reuse, not candidate acceptance or destination reuse.
4. Fence the last local computation before reusing or unregistering destination storage.
5. Cancellation/stale rounds invalidate inference work, not in-flight DMA; transport cleanup remains necessary.
6. Disconnect or uncertain transfer status cannot justify releasing exports on timeout. Current quarantine may retain resources until process exit; reclaimable peer-failure handling remains to be designed.

Drain closes admission before completing/cancelling tasks and transfers. Scale
roles independently. Migration requires a new session and state restoration;
old addresses, publications and EngineCore tickets cannot be reused.

## Delivery and acceptance

First connect an actual model artifact through references, Mooncake and the
consumer adapter across two hosts, checking output, in-flight cancellation,
drain and registration release. A token transfer does not validate probability
or feature methods. Next implement actual stochastic Draft q export and Target
verification, evaluate the Target distribution and measure transfer costs.
Feature methods, trees and cascades each require their models and algorithms;
adding Connector fields alone cannot establish support.

References: [vLLM speculative decoding](https://docs.vllm.ai/en/latest/features/speculative_decoding/),
[Mooncake Transfer Engine](https://kvcache-ai.github.io/Mooncake/design/transfer-engine/index.html).
At inspected vLLM baseline `1be3628`, MRV2 `RejectionSampler` accepts
`draft_logits`; the current external-candidate extension supplies tokens only.

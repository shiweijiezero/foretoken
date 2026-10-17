<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce multimodal TTFT through adaptive encoder microbatching

English | [简体中文](encoder-ttft-microbatching_zh.md)

## Observations and Motivation

A fast vision kernel can coexist with poor time to first token (TTFT): preprocessing, encoder queueing, embedding transfer, or downstream readiness can dominate. Waiting for a large batch improves utilization but spends the oldest request's latency budget. EAServe combines encoder separation with adaptive microbatch dispatch so uneven arrivals need not wait for a permanently fixed batch.

## Principles and Analysis

Dispatch on three conditions: the batch is full, an arrival-rate-derived idle gap suggests that another item is unlikely to arrive soon, or the oldest request reaches its age limit. Full batches amortize launches and improve device occupancy; the other triggers bound otherwise unproductive waiting. Arrival-rate adaptation changes dispatch timing, not encoder weights or image resolution.

For `V` visual tokens of decoder width `d` and `b` bytes per element, transfer payload is approximately `Vdb`. At `V=4096`, `d=3584`, and `b=2`, this is 28 MiB before protocol overhead. Encoder caching can remove computation while leaving that remote transfer intact.

## Design

Batch only tensors whose preprocessing is complete. Preserve request identity and input-item position through encoding, publish a completed embedding descriptor, and let prefill load all required embeddings before execution. The encoder can start another microbatch while a prior one transfers.

The cache owner tracks request references separately from physical eviction. Releasing a reference makes an item reclaimable; it need not delete the tensor immediately. Same-pass reuse suppresses an obsolete eviction notification. Cancellation removes references, and outstanding transfers finish or stop before their buffers are reused.

## Implementation

The dispatch and handoff are:

```text
on processed item: enqueue(request, input_position, tensor, arrival)
estimate arrival_rate from recent arrivals
if full(queue) or idle_gap(arrival_rate) or oldest_age >= age_limit:
    batch = take_compatible_ready_items(queue, encoder_capacity)
    embeddings = encoder(batch.tensors)
    descriptors = publish(embeddings, batch.request_and_position)
    start_transfer(descriptors, prefill_destinations)
for a prefill request whose required transfers are complete:
    bind_embeddings_in_input_order(request)
    execute_prefill(request)
```

Use released VLM weights and their processor; no retraining is involved. EAServe describes CUDA, NIXL, and libsmctrl but provides no author code link. vLLM's separate encoder path exposes ExampleConnector and ECMooncakeConnector; Mooncake uses TCP or RDMA. Its documented audio scope is standalone audio, not video with an audio track.

## Impact and Results

EAServe's LLaVA comparison adds simple batch-8 microbatching to Dynamo: throughput rises from 0.75 to 1.09 requests/s and mean TTFT falls from 367.4 to 112.1 ms, while mean TPOT rises from 179 to 411 ms. Better encoder batching can therefore coexist with worse downstream token latency.

On the primary eight-RTX-6000-Ada-48GB testbed, the complete EAServe system reports moderate-tier image goodput of 1.93 requests/s versus vLLM's 1.11 and Dynamo's 0.95, using LLaVA-v1.6-34B and Poisson arrivals in the 1–5 requests/s study. This includes allocation, partial prefill offload, and SM partitioning, not just adaptive batching. Waiting, embedding memory, transport, and decoder readiness remain the principal costs.

## References

- [EAServe, arXiv:2609.31551v1](https://arxiv.org/html/2609.31551v1): September 2026 microbatch and transfer method; public author code not identified.
- [vLLM encoder disaggregation](https://docs.vllm.ai/en/latest/features/disagg_encoder/): current flow, connectors, examples, and audio restrictions.
- [Encoder-cache manager at `6849ca6`](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/encoder_cache_manager.py): embedding lifetime and eviction.

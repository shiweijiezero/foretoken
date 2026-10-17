<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Diagnose KV handoff stalls through fan-in transfer profiling

English | [简体中文](kv-transfer-profiling_zh.md)

## Observations and Motivation

A fast NIC does not guarantee fast P/D handoff. KV movement includes source lookup, registration or host staging, descriptors, shards, layout conversion, and completion visibility. Several prefill producers can saturate one decode consumer or compete with expert traffic. Fan-in profiling explains exposed handoff stalls that a pairwise bandwidth peak cannot.

## Principles and Analysis

Ordinary full-attention GQA has logical cache payload `K≈2*S*L*H_KV*d*b`: tokens, layers, KV heads, head dimension, and bytes per element. The factor two is K and V. Padding, scales, replication, and framing add bytes; MLA, sliding windows, and hybrid states need their actual representation.

For `S=32768`, `L=32`, `H_KV=8`, `d=128`, and BF16 `b=2`, payload is 4 GiB. At an ideal effective 50 GB/s, transfer alone takes about 86 ms. Ten such requests/s demand approximately 43 GB/s. These are arithmetic bounds, not measurements. FP8 KV roughly halves element bytes; FP8 weights alone do not.

Decompose `T_X=Q_X+setup+pack+K/B_effective+unpack+completion`. Effective bandwidth varies with message size and competing flows; line rate omits setup and queueing.

## Design

Correlate request, source/destination shards, descriptors, transport submission, and completion events. Compare fan-in against the actual cache layout, including TP replication or conversion. Separate setup from payload execution so registration cost does not masquerade as low bandwidth.

The producer retains source pages until transport will no longer read them; the consumer retains destination pages for decode. Cancellation or failure must quiesce transfers before reuse. Layerwise overlap can shorten exposed transfer delay but extends buffer lifetime. GPU/NIC affinity, NUMA, PCIe, and rail count determine the path; host-staged TCP adds GPU↔host copies and pinned-memory pressure compared with device RDMA.

## Implementation

The handoff and its trace are:

```text
derive transfer segments from the actual KV layout and TP shards
allocate compatible destination pages and retain source references
record setup, registration, packing, and request identifiers
submit shard transfers with source/destination correlation
record completion per shard and any unpack/conversion work
mark only completed usable state ready for decode
release source references after transport no longer reads them
on cancellation or failure: stop outstanding access before buffer reuse
aggregate useful bytes and exposed wait by fan-in and request size
```

NIXLBench provides pairwise, many-to-one, and one-to-many patterns, DRAM/VRAM segments, backends, and latency percentiles. It measures transport rather than implementing service overlap. Foretoken's E/P/D example uses Mooncake RDMA or host-staged TCP for KV; encoder state uses shared storage there. Its workflow owns cross-stage cleanup.

## Impact and Results

The trace distinguishes transfer saturation from setup, packing, or decode-readiness stalls. More concurrent transfers improve utilization only until shared-link or consumer queueing dominates. Packing, staging, repeated sends, and retained pages can consume the capacity apparently saved by a fast NIC.

The AFD design-space paper's 450-versus-25 GB/s contrast is a modeled NVLink/InfiniBand placement condition, not a measured portable KV speedup. MegaScale-Infer's BF16 M2N measurements concern per-layer activations, not one-time P/D KV. Chunking hides only the part of transfer overlapped with useful computation; starting decode before required shards are ready merely relocates waiting into decode stalls. Profiling itself introduces instrumentation cost but does not alter model weights or quality.

## References

- [NIXLBench's README](https://github.com/ai-dynamo/nixl/blob/01656768741dc87922a2045feff72921e193d981/benchmark/nixlbench/README.md)
- [AFD design-space paper](https://arxiv.org/html/2605.28302v1)
- [MegaScale-Infer](https://arxiv.org/html/2504.02263)
- [E/P/D deployment guide](../../../../examples/encoder-prefill-decode/README.md)
- [workflow](../../../../data-plane/frontend/src/server/src/runtime/workflow.rs)
- [parallelism and communication](../parallelism-communication/)

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Inference Knowledge

English | [简体中文](README_zh.md)

Inference mechanisms, implementations, and research questions, organized by the computation or resource being optimized.

## Model computation

- [Attention and recurrent state](attention-state/): dense attention pipelines, MLA layouts, sparse indexing, channel-gated recurrence, and state reuse.
- [MoE execution](moe-execution/): expert work partitioning, token assignment, replica balancing, and quantized execution layouts.
- [Speculative decoding](speculative-decoding/): draft generation, verification, acceptance cost, adaptive depth, and candidate selection.
- [Quantization](quantization/): block-scaled formats, outliers, calibration, and the interaction between numerical error and available kernels.
- [Multimodal and generative models](multimodal-generation/): visual encoding, multimodal batches, diffusion acceleration, and streaming generation.
- [Multiple models and adapters](multi-model-adapters/): LoRA computation, adapter residency, tenant scheduling, and shared execution capacity.

## Device execution

- [Kernel design](kernel-design/): persistent work assignment, warp-specialized pipelines, layout-aware fusion, and vocabulary selection.
- [Compilation and runtime](compiler-runtime/): shape specialization, graph dispatch, custom operator boundaries, and graph memory lifetimes.
- [Parallelism and communication](parallelism-communication/): tensor, expert, context, and pipeline parallelism; communication granularity and overlap.
- [Heterogeneous hardware](heterogeneous-hardware/): architecture-specific layouts, graph and stream behavior, collectives, and CPU/GPU execution choices.

## Serving and resource management

- [Engine scheduling](engine-scheduling/): token budgets, prefill/decode interference, preemption, fairness, and layer-group prefill.
- [KV caching](kv-cache/): storage tiers, partial reuse, compression, selection, and transfer compatibility.
- [Request routing and admission](routing-admission/): cache affinity versus queueing, predicted work, deadlines, and overload.
- [Disaggregated serving](disaggregated-serving/): P/D and attention/FFN separation, stage balance, transfers, and heterogeneous stage placement.
- [Autoscaling and placement](autoscaling-placement/): capacity estimation, feedback delay, cold starts, topology, and safe capacity removal.
- [Model loading and storage](model-loading-storage/): loading pipelines, I/O and host memory, distributed weights, and sleep/wake recovery.

## Host execution and evidence

- [Host and streaming paths](host-streaming/): host/device scheduling, incremental text, backpressure, cancellation, and NUMA effects.
- [Measurement and simulation](measurement-simulation/): arrivals and queueing, SLO goodput, trace critical paths, simulator calibration, and numerical divergence.

A decode slowdown can involve several categories. For example, use attention or MoE entries to understand computation, communication entries to inspect rank dependencies, and host/runtime entries to explain launch gaps.

## References

Implementation status refers to the cited version; reported results retain their source and experimental conditions, with Foretoken measurements linked to experiment records.

Entries distinguish existing capabilities from open questions, and execution-only changes from methods requiring new weights, training, or model changes.

KV-cache, model-loading, and quantization notes use the following source snapshots; specific implementation paths are linked in the notes.

- [LMCache](https://github.com/LMCache/LMCache/tree/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224)
- [Mooncake](https://github.com/kvcache-ai/Mooncake/tree/342c8aeece6b4eefb76c6b8b31ccfac8dde07aa3)
- [vLLM](https://github.com/vllm-project/vllm/tree/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9)
- [Dynamo](https://github.com/ai-dynamo/dynamo/tree/7708f041a965e35374af976ab8ad610b9537e3bc)
- [ModelExpress](https://github.com/ai-dynamo/modelexpress/tree/db0cbce97184d9135d35c7290711f3053b220b6a)
- [llm-compressor](https://github.com/vllm-project/llm-compressor/tree/990fdd2fb41af48f105dbdebd088a96b627701b1)
- [NVIDIA Model Optimizer](https://github.com/NVIDIA/Model-Optimizer/tree/b5f10f8bf96201241c35b86498557bd6fdde10ca)

## Add or update knowledge

Use one category directory per technical responsibility and one English file per question with a `_zh.md` counterpart. Use a short target object and core technique for the file name, not a full sentence; let the title express the complete goal and method. The knowledge index links category directories, which contain the notes directly. An entry can compare several papers or implementations; a project name alone is not a reason to create a new category or another note.

The [knowledge template](../../templates/guidance-template.md) organizes observations and motivation, principles and analysis, design, implementation, impact and results, and references. Extend an existing entry when new evidence changes the same question. Preserve explanations of failed approaches and reusable inputs or code, and update the knowledge index when adding a category. Research methods themselves belong in [meta-knowledge](../meta-knowledge/README.md); run-specific commands and raw results belong in [experiment records](../../experiment-records.md).

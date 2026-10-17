<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance Pipeline Latency and Throughput Through Microbatching

English | [简体中文](pipeline-microbatch-balance_zh.md)

## Observations and Motivation

Pipeline parallelism (PP) partitions layers instead of splitting every weight matrix. It can fit a model across nodes and keep frequent tensor-parallel collectives within each node. But reducing collective frequency does not guarantee lower single-request latency: one request still traverses all stages, and autoregressive feedback adds a dependency before its next token.

## Principles and Analysis

Independent requests or legal microbatches can occupy different stages simultaneously. For `P` equally timed stages, `M` independent forward microbatches, and stage time `t`, an ideal forward-only pipeline takes `(M+P-1)*t`; aggregate stage utilization is `M/(M+P-1)`. This ignores transfers, unequal stage cost, and service scheduling. It is not a training 1F1B formula and not a claim about vLLM's actual schedule.

With `P=4,M=1`, the single microbatch traverses four stages serially. With eight independent microbatches, the ideal schedule uses eleven stage-time slots. These examples explain fill/drain cost; they do not predict latency under continuous arrivals. More in-flight work can improve throughput while increasing how long an individual request waits.

## Design

Keep frequent TP collectives within a fast domain and assign consecutive layer ranges to PP stages. Each stage owns its weights and attention-layer KV; stage messages include the model's actual intermediate tensors, potentially residual streams as well as hidden states.

Balance stage execution rather than layer counts: embeddings, output projection, context-dependent attention and MoE skew make stages unequal. Independent requests can occupy multiple stages; chunks and decode tokens from one sequence retain cache and sampling dependencies. Use the engine's supported uneven splits rather than arbitrary module relocation.

## Implementation

A forward-only inference pipeline executes:

1. Stage zero admits a ready request/microbatch and runs its local layers.
2. Send the exact intermediate tensor set to the next stage; that stage waits for readiness, runs its layers and forwards the result.
3. Keep each independent microbatch's identity, positions and cache mapping through every handoff.
4. The final stage computes logits/sampling. Return the next token and required scheduling state before admitting that sequence's next decode step.
5. Reuse activation slots only after their sends and consumers complete; cancellation coordinates all stages holding the request.

vLLM implements PP, combined TP/PP and supported uneven layer splits. DeepEP's experimental `PPBuffer` is a transport primitive: it requires an RDMA-only group, contiguous CUDA tensors, 32-byte pointer/size alignment, and preconfigured maximum bytes/in-flight slots. It does not replace the serving schedule.

## Impact and Results

PP expands model capacity across nodes and replaces some cross-node TP synchronization with activation handoffs. At sufficient concurrency, independent work fills stages and improves aggregate utilization. At low concurrency, a request still traverses all stages and waits for autoregressive feedback, so fewer collectives do not guarantee lower TPOT.

More in-flight microbatches trade occupancy against per-request queueing and buffer capacity. Uneven splits can reduce the slowest-stage bottleneck, while deeper pipelines add fill/drain bubbles and broader cancellation/failure coordination. The ideal utilization formula explains these dependencies rather than providing measured service throughput.

## References

- [parallelism guide at 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/serving/parallelism_scaling.md)
- [main README at 93eb6eb2](https://github.com/deepseek-ai/DeepEP/tree/93eb6eb238127e96c6d7a4a625a6dad158348509)
- [TP collective selection](tp-collective-selection.md)

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Scale Long-Context Attention Through Query and KV Sharding

English | [简体中文](context-query-kv-sharding_zh.md)

## Observations and Motivation

Long-context prefill distributes many query tokens; decode distributes access to a large history for very few new queries. They need different communication patterns. Calling both sequence or context parallelism (CP) can hide whether the implementation shards queries, heads, KV history, or only intermediate activations.

## Principles and Analysis

Ulysses starts with sequence-sharded Q/K/V. An all-to-all transposes ownership so each rank receives a longer sequence for fewer heads. Local attention processes those heads, and an inverse all-to-all restores sequence ownership. Head divisibility limits the straightforward implementation, especially with few KV heads or TP already dividing them.

Ring attention keeps query shards local and circulates KV blocks. A numerically stable running softmax combines blocks without storing the full score matrix. Ulysses redistributes heads; ring exchange distributes KV access, so their combination separates two communication dimensions.

## Design

Unified Sequence Parallelism (USP) creates orthogonal Ulysses and ring groups; their degrees multiply to total sequence parallelism, for example two by four across eight ranks. Put frequent all-to-all in a fast domain and use ring exchange across another domain when block computation can hide it. Zigzag pairs early and late causal query chunks to balance work while preserving original positions, RoPE and masks.

Decode context parallelism (DCP) instead shards KV history within an existing TP group and communicates query/output information. For one KV head and TP eight, this can replace eight duplicated histories with distributed ownership without adding GPUs. Prefill context parallelism (PCP) query sharding does not necessarily shard persistent KV: the cited PCP-only decode path gathers new KV to maintain complete replicas, while a DCP combination retains replicated decode queries.

## Implementation

For USP prefill:

1. Permute query chunks into the supported causal-balanced order, retaining original position IDs.
2. All-to-all Q/K/V within the Ulysses group to exchange sequence ownership for head ownership.
3. Circulate K/V blocks within the ring group. For each valid block, update the running softmax maximum, normalization sum and value accumulator.
4. Normalize the accumulated output, inverse all-to-all and undo the query permutation.

For DCP decode, each owner computes normalized local attention `o_r` and log-normalizer `lse_r`. Merge as:

```text
L = logsumexp_r(lse_r)
o = sum_r exp(lse_r - L) * o_r
```

Skip empty shards (`lse_r = -inf`) rather than multiplying undefined local outputs. Head divisibility, TP/KV-head relationships, paged-cache backend and speculative-query support determine eligibility. YunChang's forward-only alternatives do not themselves provide serving-cache integration; vLLM's guide marks prefill strategies as under development.

## Impact and Results

Query sharding distributes long-prefill attention work, while causal-balanced placement reduces the slowest-rank imbalance. Its costs are ownership transposes, ring traffic and temporary buffers. Short sequences can spend more on launch/reshape than they save in computation.

DCP reduces duplicated KV and expands usable history/request capacity, but adds query/output communication and softmax merging. More capacity can enable higher throughput without making a single decode step faster. USP's original BF16/FP16 training and attention results describe a different workload from continuous paged decode.

## References

- [USP paper](https://arxiv.org/abs/2405.07719)
- [YunChang a5f8f600](https://github.com/feifeibear/long-context-attention/tree/a5f8f600d435ced180a97c91c68e87ff3cfb8f89)
- [CP guide at 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/serving/context_parallel_deployment.md)
- [PCP cache operations](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/attention/ops/pcp.py)

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Meet adapter deadlines through rank- and slack-aware scheduling

English | [简体中文](adapter-deadline-scheduling_zh.md)

## Observations and Motivation

Adapters sharing one base model do not have equal serving cost. A rank-128 tenant can consume more compute and memory than rank 8, while tenants with different first-token and inter-token deadlines cannot be judged through one average latency. LoRAServe addresses rank-aware distributed placement and routing; HALO addresses worker scheduling and decode execution overlap.

## Principles and Analysis

LoRAServe estimates token demand from recent traffic and divides it by pre-profiled rank-specific capacity under the target SLO. Rank budgets are packed into server assignments, with fractional routing summing to one for each adapter. Grouping similar ranks reduces heterogeneous-batch inefficiency; minimizing assignment changes reduces migration.

HALO computes slack to each request's next deadline: TTFT before the first token, then TBT for later tokens. Already generated tokens in an output queue create room to defer a decode iteration. Independently, base and LoRA operators can consume the same layer input concurrently and combine their outputs. SM partitioning can shorten decode computation, whereas output buffering changes delivery timing.

## Design

Keep distributed placement and worker execution separate. Placement owns rank-dependent capacity and adapter demand assignments. The worker prioritizes urgent prefill/decode work and can skip requests with sufficient buffered-token slack. Its output queue retains ordering and releases tokens according to the delivery schedule.

HALO profiles SM allocations for base and adapter paths rather than assuming equal partitioning. Its overlap targets decode, not prefill. Both methods use existing compatible base and trained LoRA weights; target modules, rank, scale, and tokenizer remain unchanged. LoRAServe's A100 setup uses InfiniBand/GPUDirect RDMA for remote adapter fetches; HALO requires GPU/runtime SM controls.

## Implementation

The two scheduling levels can be summarized as:

```text
estimate token demand for each adapter from recent arrivals
normalize demand by its profiled rank-specific SLO capacity
pack adapter budgets into servers; preserve assignments where possible
route each adapter using its normalized server fractions
at a worker iteration: compute slack to every next token deadline
select urgent prefill and decode requests within work budgets
defer decode where buffered output leaves sufficient slack
run base and LoRA decode paths with profiled SM allocations
combine updates, enqueue generated tokens, release in order
```

This is a conceptual composition of two paper mechanisms, not a joint released scheduler. Neither cited paper version supplies an author code link. Remote residency and the existing engine's batch limits still constrain selected work; a request cannot execute merely because its deadline is urgent.

## Impact and Results

LoRAServe reports up to 2× throughput over S-LoRA and 20% over Toppings under its service constraints, using A100 servers, Llama models, and adapters of ranks 8–128. Most experiments use four servers; several use a 10 s P95 TTFT target. Its rank-isolation motivation reports rank-8 P95 TTFT increasing 84% when co-served with rank 128.

HALO, evaluated on A100 80GB with Llama3-8B/Qwen2.5-7B and four-A100 configurations for 34B/32B models, reports 8.3% average decode improvement from base–LoRA parallelism, up to 14.7%; it does not report the same prefill benefit. Its full multi-SLO system improves constrained throughput 19.5–29.5% over QoServe, VTC-R, VTC-T, and SLoRA in the stated scenario. Those system gains also include scheduling and buffering. Rank grouping can increase waiting, and urgent service can consume capacity otherwise available to relaxed or rare tenants.

## References

- [LoRAServe, arXiv:2511.22880v1](https://arxiv.org/html/2511.22880v1): method, remote-transfer path, and evaluated workloads. This version promises code/traces after acceptance but supplies no author repository.
- [HALO, arXiv:2610.04956v1](https://arxiv.org/html/2610.04956v1): submitted 2026-10-04; slack scheduling, SM search, and output queue. No author code link identified in this version.
- [SGLang LoRA documentation](https://docs.sglang.io/advanced_features/lora.html): existing multi-adapter batching and runtime restrictions; source main at `e9400c5`.

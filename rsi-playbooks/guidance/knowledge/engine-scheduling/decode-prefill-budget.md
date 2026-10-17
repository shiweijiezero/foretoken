<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Limit Decode Stalls Through Token-Budgeted Prefill Chunks

English | [简体中文](decode-prefill-budget_zh.md)

## Observations and Motivation

Long prefills sharing a GPU with active generation can produce long token-delivery gaps. Splitting the prompt limits each interruption, but a small fixed chunk can waste compute and repeat scheduling and memory traffic. The target is enough prefill per iteration without delaying the next decode token beyond its delivery budget.

## Principles and Analysis

Memory-bound decode and compute-heavy prefill can share a hybrid batch. Sarathi-Serve bounds prefill work so the mixed iteration remains short. The relevant constraint is:

`iteration_time(decode_population, context_lengths, prefill_tokens) <= delivery_budget`.

The delivery budget is the time remaining for active streams after reserving host scheduling and response delivery. Equal token counts can take different times because of context length, MoE expert activation, collectives and speculative verification. GPU tile sizes also matter: a slightly smaller budget can select a less efficient matrix shape.

For example, 64 ordinary one-token decodes leave 448 prefill tokens in a 512-token budget. An 8,192-token prompt then needs at least 19 iterations; a 2,048-token budget leaves 1,984 tokens and needs at least five. These are accounting examples, not latency measurements.

## Design

Reserve work for ongoing decode, then fill the remaining token and latency capacity with uncached prefill suffixes. Larger chunks improve prefill efficiency and TTFT; smaller chunks preserve decode opportunities. Sarathi profiles a fixed budget against tail token spacing. SOLA instead uses TTFT/TPOT pressure to adjust workload and ordering, correcting fitted costs with observed iteration time.

The engine owns chunk progress, token selection, KV allocation and graph-compatible execution. The request layer selects instances and tracks requests; cluster orchestration supplies capacity rather than generating iteration schedules.

## Implementation

1. Read running requests, remaining prompt work, token/input limits and reclaimable KV capacity.
2. Select ongoing decode work using the engine's actual accounting, including draft slots and verification tokens.
3. Choose a prefill chunk that fits both residual capacity and the latency surface calibrated for the model, weight/cache dtype, backend and parallelism.
4. Allocate its KV slots and execute the mixed batch. Advance computed-token progress only for executed work.
5. For adaptive scheduling, update the execution-cost estimate from the measured iteration and use the new SLO pressure in the next selection.

vLLM V1 already implements chunked prefill and running-before-waiting scheduling, with encoder budgets and hybrid-state alignment. Those constraints remain part of selection.

## Impact and Results

Chunking limits long-prefill monopolization while using compute headroom alongside decode. It trades prefill completion time against individual generation gaps. Smaller chunks add iterations, attention reads and collectives; MoE can also reread expert weights, motivating [layered prefill](moe-layered-prefill.md). Pure decode has no prefill interference to remove, and high decode concurrency can consume the entire delivery budget.

## References

- [Sarathi-Serve, arXiv 2403.02310v1](https://arxiv.org/html/2403.02310v1): stall-free hybrid batches, profiled token budget, and tile quantization.
- [SOLA, MLSys 2025, §§4–5](https://proceedings.mlsys.org/paper_files/paper/2025/file/bc82dbfbfa43232be85b8d9838f49c3e-Paper-Conference.pdf): iteration-level workload control and online cost correction; evaluated on a vLLM 0.4.2 integration.
- [vLLM optimization guide](https://docs.vllm.ai/en/latest/configuration/optimization/): current chunking and preemption guidance.
- [vLLM V1 scheduler, commit 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/sched/scheduler.py): `schedule`, token/input budgets, and allocation constraints.

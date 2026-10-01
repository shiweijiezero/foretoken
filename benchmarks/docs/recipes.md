<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Experiment command reference

English | [简体中文](recipes_zh.md) · [Evaluation and profiling](../README.md)

Run these commands from the repository root, replacing `examples/quickstart` with your model's configuration directory.

## Input length, output length, and concurrency

Compare latency and throughput across fixed input/output length pairs and concurrency levels, keeping only pairs that fit the model's context.

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/fixed-length.jsonl \
  --num-prompts 32 --warmup-requests 4 --num-runs 1 --temperature 0 \
  --experiment-name fixed-length --output local,wandb,plot
```

## Long-context performance

Vary input length at concurrency 1 with a fixed 512-token output, reserving room for the output within the model's context.

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/long-context.jsonl \
  --max-concurrency 1 --min-output-length 512 --max-output-length 512 \
  --num-prompts 4 --warmup-requests 1 --num-runs 1 --temperature 0 \
  --experiment-name long-context --output local,wandb,plot
```

## Conversations started per second

Use ShareGPT and StudyChat to start conversations using a Poisson process, averaging 2, 4, 8, or 16 new conversations per second; `--num-prompts` counts individual turns.

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/conversation-rate.jsonl \
  --max-concurrency -1 --temperature 0 --random-seed 0 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name sharegpt-rate --output local,wandb,plot

foretoken perf examples/quickstart \
  --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
  --sweep benchmarks/scripts/common/conversation-rate.jsonl \
  --max-concurrency -1 --temperature 0 --random-seed 0 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name studychat-rate --output local,wandb,plot
```

## SLO attainment and goodput

Measure the fraction of requests meeting both latency targets and the throughput of requests or tokens that meet them.

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/conversation-rate.jsonl \
  --slo-params '[{"ttft":"<=2s","tpot":"<=100ms"}]' \
  --max-concurrency -1 --temperature 0 --random-seed 0 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name slo-attainment --output local,wandb,plot
```

## SLO threshold sensitivity

Vary the average number of conversations started per second and the first-token latency threshold, using Poisson arrivals to compare attainment and goodput.

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/slo-thresholds.jsonl \
  --max-concurrency -1 --temperature 0 --random-seed 0 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name slo-thresholds --output local,wandb,plot
```

## Timestamped trace replay

Replay eight-minute StudyChat and Mooncake windows to compare latency, throughput, and replay delay; Mooncake reconstructs shared prefixes for cache experiments.

```bash
foretoken perf examples/quickstart \
  --trace KrisQ/StudyChat --dataset KrisQ/StudyChat \
  --trace-start 18609050.546 --trace-duration 480 \
  --max-concurrency 16 --max-tokens 4096 \
  --output local,wandb,plot

foretoken perf examples/quickstart \
  --trace valeriol29/mooncake-traces:conversation --dataset random \
  --trace-start 57 --trace-duration 480 --max-concurrency 16 \
  --trace-synthetic-prefix-reuse --random-seed 0 --max-tokens 64 \
  --output local,wandb,plot
```

## Task accuracy

Score five zero-shot tasks with up to 100 samples per task.

```bash
foretoken eval examples/quickstart \
  --tasks piqa,arc_easy,arc_challenge,hellaswag,winogrande \
  --num_fewshot 0 --limit 100 --output local,wandb,plot
```

## Perplexity

Measure WikiText perplexity using a service that returns input-token log probabilities.

```bash
foretoken eval examples/quickstart \
  --tasks wikitext --limit 100 --output local,wandb,plot
```

## Quantization: performance and quality

Compare the BF16 and 4-bit example deployments on the same performance workload and evaluation tasks.

```bash
foretoken perf --dataset random \
  --sweep benchmarks/scripts/common/quantized-models.jsonl \
  --num-prompts 100 --warmup-requests 10 --num-runs 3 --temperature 0 \
  --experiment-name quantization --output local,wandb,plot

foretoken eval examples/quantized-model/bf16 examples/quantized-model/bitsandbytes \
  --tasks piqa,hellaswag --num_fewshot 0 --limit 100 \
  --output local,wandb,plot
```

## Output distribution comparison

Compare full-vocabulary KL divergence and token agreement against BF16; the candidate file supplies labels and weight precision for the plots.

```bash
foretoken eval --reference examples/quantized-model/bf16 \
  --candidates examples/quantized-model/candidates.jsonl \
  --context-length 512 --num-windows 4 --score-tokens 16 \
  --output local,wandb,plot
```

## Speculative decoding

Set `BASELINE` and `CANDIDATE` to configuration directories for the same target model without and with speculative decoding, then compare performance and greedy token sequences.

```bash
BASELINE=path/to/non-speculative-deployment
CANDIDATE=path/to/speculative-deployment

foretoken perf "$BASELINE" "$CANDIDATE" --dataset random \
  --sweep benchmarks/scripts/common/fixed-length.jsonl \
  --num-prompts 32 --warmup-requests 4 --num-runs 1 --temperature 0 \
  --experiment-name speculative-decoding --output local,wandb,plot

foretoken eval "$CANDIDATE" --reference "$BASELINE" \
  --greedy-compare --context-length 512 --num-windows 8 --max-tokens 128 \
  --output local,wandb,plot
```

## Cache, deployment, and scaling ablations

Set `BASELINE` and `CANDIDATE` to two deployments that differ in the mechanism under study, and replay the same workload on each.

```bash
BASELINE=path/to/baseline-deployment
CANDIDATE=path/to/ablation-deployment

foretoken perf "$BASELINE" "$CANDIDATE" \
  --trace valeriol29/mooncake-traces:conversation --dataset random \
  --trace-start 57 --trace-duration 480 --max-concurrency 16 \
  --trace-synthetic-prefix-reuse --random-seed 0 --max-tokens 64 \
  --slo-params '[{"ttft":"<=2s","tpot":"<=100ms"}]' \
  --output local,wandb,plot
```

## Redraw figures

Read saved results to change figure width or select a metric without rerunning inference.

```bash
foretoken plot results/fixed-length --columns 2

foretoken plot results/fixed-length --metric latency_p95_seconds \
  --output-dir results/fixed-length/latency-figure
```

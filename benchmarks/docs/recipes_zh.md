<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 实验命令参考

[English](recipes.md) | 简体中文 · [评测与性能剖析](../README_zh.md)

在仓库根目录运行，将 `examples/quickstart` 换成待测模型的配置目录。

## 输入输出长度与并发

在固定输入／输出长度组合下扫描并发，比较延迟与吞吐量；配置文件中只保留模型上下文能容纳的长度组合。

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/fixed-length.jsonl \
  --num-prompts 32 --warmup-requests 4 --num-runs 1 --temperature 0 \
  --experiment-name fixed-length --output local,wandb,plot
```

## 长上下文性能

固定并发为 1、输出为 512 token，比较输入长度增长带来的性能变化；选择输入长度时，为输出预留 512 token。

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/long-context.jsonl \
  --max-concurrency 1 --min-output-length 512 --max-output-length 512 \
  --num-prompts 4 --warmup-requests 1 --num-runs 1 --temperature 0 \
  --experiment-name long-context --output local,wandb,plot
```

## 多轮对话：每秒启动多少段

使用 ShareGPT 和 StudyChat，按泊松过程随机启动新对话，分别测试平均每秒 2、4、8、16 段对话；`--num-prompts` 按每轮请求计数。

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

## SLO 达标率与 goodput

统计同时满足两项延迟目标的请求比例，以及达标请求或 token 的吞吐量。

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/conversation-rate.jsonl \
  --slo-params '[{"ttft":"<=2s","tpot":"<=100ms"}]' \
  --max-concurrency -1 --temperature 0 --random-seed 0 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name slo-attainment --output local,wandb,plot
```

## SLO 阈值敏感性

改变平均每秒启动的对话数和首 token 延迟阈值，比较达标率和 goodput；新对话按泊松过程随机启动。

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/slo-thresholds.jsonl \
  --max-concurrency -1 --temperature 0 --random-seed 0 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name slo-thresholds --output local,wandb,plot
```

## 真实时间戳回放

分别回放 StudyChat 和 Mooncake 的八分钟窗口，比较延迟、吞吐与发送延后；Mooncake 重建共享前缀，可用于缓存实验。

```bash
foretoken perf examples/quickstart \
  --trace KrisQ/StudyChat --dataset KrisQ/StudyChat \
  --trace-start 18609050.546s --trace-duration 8min \
  --max-concurrency 16 --max-tokens 4096 \
  --output local,wandb,plot

foretoken perf examples/quickstart \
  --trace valeriol29/mooncake-traces:conversation --dataset random \
  --trace-start 57s --trace-duration 8min --max-concurrency 16 \
  --trace-synthetic-prefix-reuse --random-seed 0 --max-tokens 64 \
  --output local,wandb,plot
```

## 任务准确率

对五个零样本任务分别评测最多 100 个样本。

```bash
foretoken eval examples/quickstart \
  --tasks piqa,arc_easy,arc_challenge,hellaswag,winogrande \
  --num_fewshot 0 --limit 100 --output local,wandb,plot
```

## 困惑度

使用支持输入 token 对数概率的服务，测量 WikiText 困惑度。

```bash
foretoken eval examples/quickstart \
  --tasks wikitext --limit 100 --output local,wandb,plot
```

## 量化性能与质量

用相同的性能负载和评测任务，比较示例中的 BF16 与 4-bit 部署。

```bash
foretoken perf --dataset random \
  --sweep benchmarks/scripts/common/quantized-models.jsonl \
  --num-prompts 100 --warmup-requests 10 --num-runs 3 --temperature 0 \
  --experiment-name quantization --output local,wandb,plot

foretoken eval examples/quantized-model/bf16 examples/quantized-model/bitsandbytes \
  --tasks piqa,hellaswag --num_fewshot 0 --limit 100 \
  --output local,wandb,plot
```

## 输出分布对比

以 BF16 为参考比较完整词表 KL 散度和 token 一致率，候选文件提供图表所用的模型名称与权重位宽。

```bash
foretoken eval --reference examples/quantized-model/bf16 \
  --candidates examples/quantized-model/candidates.jsonl \
  --context-length 512 --num-windows 4 --score-tokens 16 \
  --output local,wandb,plot
```

## 推测解码

将 `BASELINE` 和 `CANDIDATE` 分别设为同一目标模型关闭、开启推测解码的配置目录，比较性能和贪心生成序列。

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

## 缓存、部署与扩缩容消融

将 `BASELINE` 和 `CANDIDATE` 设为仅改变待研究机制的两个部署目录，分别回放同一负载进行比较。

```bash
BASELINE=path/to/baseline-deployment
CANDIDATE=path/to/ablation-deployment

foretoken perf "$BASELINE" "$CANDIDATE" \
  --trace valeriol29/mooncake-traces:conversation --dataset random \
  --trace-start 57s --trace-duration 8min --max-concurrency 16 \
  --trace-synthetic-prefix-reuse --random-seed 0 --max-tokens 64 \
  --slo-params '[{"ttft":"<=2s","tpot":"<=100ms"}]' \
  --output local,wandb,plot
```

## 重新绘图

读取已保存的结果，调整图宽或选择指标，无需重新推理。

```bash
foretoken plot results/fixed-length --columns 2

foretoken plot results/fixed-length --metric latency_p95_seconds \
  --output-dir results/fixed-length/latency-figure
```

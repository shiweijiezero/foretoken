<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 回放 AgentX 负载

[English](agentx.md) | 简体中文 · [性能评测示例](README_zh.md)

使用 `foretoken perf --agentx`，通过官方 AgentX 会话回放测量模型服务性能。负载保留长上下文的前缀复用、多轮顺序、并行子 agent 和工具等待时间；并发数表示活跃的 agent 会话数。

## 运行评测

完成[通用准备](../../README_zh.md#开始使用)，准备上下文窗口和内存容量能够承载所选语料的模型服务。默认语料 `semianalysis_cc_traces_weka_062126` 包含最长 1M token 的上下文；其 `_256k` 版本保留输入加输出不超过 256k token 的请求。

将 `path/to/deployment` 换成该服务的 Kustomize 目录。下面选择 256k 语料：

```bash
foretoken perf path/to/deployment --agentx \
  --dataset semianalysis_cc_traces_weka_062126_256k \
  --max-concurrency 16 --output local,wandb,plot
```

Foretoken 从部署中解析模型、tokenizer、地址、Gateway 路由请求头和声明的 GPU 容量。官方场景先预热，再执行默认一小时的 profiling 窗口；每个扫描点和每次重复测量分别预热和计时。

首次执行时，Foretoken 通过 uv 在独立缓存的 Python 3.13 环境中准备 AgentX Harness 1.0.12，并在需要时下载 tokenizer 和语料；后续运行复用这些缓存。选择 W&B 输出前执行 `wandb login`，或用 `--output local,plot` 仅保存本地结果和图表。

评测已有 API 时，将 `MODEL_SERVICE_URL` 设为完整的 Chat Completions 地址，`MODEL_ID` 设为服务公开的模型名称：

```bash
foretoken perf --agentx --url "$MODEL_SERVICE_URL" --model "$MODEL_ID" \
  --dataset semianalysis_cc_traces_weka_062126_256k \
  --max-concurrency 16 --output local,wandb,plot
```

需要认证时添加 `--api-key`。模型别名可能需要通过 `--tokenizer-path` 指定 Hugging Face tokenizer 或本地 tokenizer 目录。使用已有 Foretoken 前端 URL 时，添加 `--extra-body '{"include_reasoning":true}'`，让计时客户端能够观察 reasoning token；部署目录方式会自动请求这些内容。

## 比较配置

传入多个部署目录，或多个 URL 及一个共用模型名或逐 URL 模型名，可以依次测量不同方案。用[参数扫描格式](sweep_zh.md)改变回放设置，例如将下面内容保存为 `agentx-sweep.jsonl`：

```json
{"max_concurrency":[1,4,16]}
```

```bash
foretoken perf path/to/deployment --agentx \
  --dataset semianalysis_cc_traces_weka_062126_256k \
  --sweep agentx-sweep.jsonl --num-runs 3 \
  --output local,wandb,plot
```

AgentX 使用 trace 记录的响应长度。`foretoken perf --agentx --help` 列出 `--max-context-length`、`--random-seed`、`--warmup-requests-per-lane` 等回放控制；未指定的设置采用官方场景默认值。语料和方法规则见[官方 AgentX 指南](https://github.com/SemiAnalysisAI/agentx-harness/blob/agentx-v1.0.12/docs/tutorials/agentx-mvp.md)。

扫描中断后，重复原命令并通过 `--resume RESULT_DIR` 指向之前打印的结果目录。已完成测量会复用，未完成点从预热重新执行；保留原结果目录。测量已完成时，运行 `foretoken perf --resume RESULT_DIR`，即可恢复结果发布或图表导出，无需重新测量。

## 查看与绘制结果

终端汇总观测到的请求、吞吐量、交互速度及原生报告的提交有效性。在打印的结果目录中查看：

| 文件 | 内容 |
| --- | --- |
| `metrics.json` | 导入的请求数、速率、归一化指标及 AgentX 有效性信息 |
| `agentx.log` | 原生客户端执行与错误日志 |
| `native/profile_export_aiperf.json` | 官方 profiling 汇总、配置及无效原因 |
| `native/profile_export.jsonl` | 原生逐请求指标，含 warmup 或 profiling 阶段标记 |

AgentX 交互速度使用成功 profiling 请求的原生 token 间隔，优先采用完整响应 ITL。绘图时将它与 Foretoken 基于 TPOT 的解码速度分别选择：

```bash
foretoken plot results/baseline results/candidate \
  --x-metric agentx_interactivity --x-stat p95 \
  --y-metric total_tokens_per_second_per_gpu \
  --frontier max-max --label-values --columns 2
```

URL 结果未记录 GPU 容量时，通过 `--gpu-count` 提供实际数量。收益选点也用 `--interactivity-metric agentx_interactivity --interactivity-stat p95` 选择同一指标。定义见[性能指标](../metrics_zh.md#agentx-指标)，图形选择、计价及 W&B 恢复见[绘图指南](../plotting_zh.md)。

## 检查开发用 trace

用 `--input-file` 选择[官方 Weka 格式](https://github.com/SemiAnalysisAI/agentx-harness/blob/agentx-v1.0.12/docs/tutorials/weka-trace.md)的本地 trace，将下面两个路径分别换成已准备的部署和 trace：

```bash
foretoken perf path/to/deployment --agentx \
  --input-file path/to/weka-trace.json --unsafe-override \
  --max-concurrency 1 --duration 30s \
  --warmup-requests-per-lane 1 --agentic-warmup-grace-period 60s \
  --record-processors 1 --output local,plot
```

`--unsafe-override` 允许探索性的场景设置，原生报告会将这些运行标记为不符合正式 AgentX 提交条件。比较或发布成绩前，查看 `submission_valid` 及其原因。AgentX 的合成回放衡量服务性能，不用于模型回答质量评分。

原生客户端停在 `Processing Records` 时，可加 `--record-processors 1` 重试。按 Ctrl+C 会中断客户端并保留可用报告和日志；较早失败的运行可能只有日志，没有完整 profiling 汇总。

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 评测模型质量

[English](README.md) | 简体中文 · [评测与性能剖析](../../README_zh.md)

使用 lm-evaluation-harness 或 EvalScope，为运行中的模型回答评分。完成[准备步骤](../../README_zh.md#开始使用)后，选择下面的框架运行。

## lm-evaluation-harness

先评测 100 道 GSM8K 数学题：

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval \
  --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 \
  --output local,wandb
```

汇总结果列出任务得分、答案提取方式、样本数，以及框架提供的标准误差。默认框架是 `lm-eval`，任务名称和参数直接采用[上游 CLI 的写法](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/interface.md)：

- `--num_fewshot 0` 使用零样本提示。
- `--log_samples` 保存逐题输入和回答。
- `--model_args num_concurrent=4` 同时发送四个 API 请求。

连接信息由 Foretoken 提供。Chat Completions 接口适用于根据生成答案评分的任务；需要候选答案对数似然的任务应改用相应的生成式变体。

## EvalScope

```bash
foretoken eval examples/quickstart \
  --evaluator evalscope \
  --model Qwen/Qwen3-0.6B \
  --datasets gsm8k --limit 100 \
  --output local,wandb
```

汇总结果展示任务得分和已完成评分的样本数，各类别和子集的详细分数保存在报告与 W&B 中。通过 [EvalScope 原生参数](https://evalscope.readthedocs.io/zh-cn/latest/get_started/basic_usage.html)配置任务，例如 `--dataset-args` 和 `--generation-config`。

两个框架都可去掉 `--limit`，运行完整的所选任务。提示词和判分规则由框架及任务定义。全部选项分别见 `foretoken eval --evaluator lm-eval --help` 和 `foretoken eval --evaluator evalscope --help`。

## 评测已有服务

将部署目录换成服务的 Chat Completions URL，并指定模型名：

```bash
foretoken eval \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --evaluator lm-eval \
  --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 \
  --output local,wandb
```

此模式不使用 Kubernetes 资源。需要认证时添加 `--api-key`。Foretoken Gateway 部署则传入 Kustomize 目录，由命令查找地址并配置路由请求头。

## 查看评分

打开命令打印的结果目录：

| 文件或目录 | 内容 |
| --- | --- |
| `metrics.json` | 任务得分、子集、答案提取方式、样本数，以及框架提供的不确定性或执行状态 |
| `native/` | 框架报告及其生成的逐样本记录 |
| `evaluator.log` | 评测框架的运行日志 |

W&B 提供任务指标、分数表，并将评测文件作为 artifact 供下载。输出位置与运行分组采用通用[结果设置](../../README_zh.md#查看和保存结果)。

比较分数时，使用相同的框架、任务配置和样本范围。

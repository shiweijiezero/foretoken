# 评测与性能剖析

[English](README.md) | 简体中文

用 `foretoken perf` 测量服务延迟和吞吐量，用 `foretoken eval` 评估回答质量，通过性能剖析定位执行瓶颈。

## 开始使用

使用 Python 3.11 或更高版本安装 Foretoken：

```bash
pip install foretoken

# 从源码目录安装：
# pip install -e .
```

以下示例在[快速开始](../README_zh.md#快速开始)准备的仓库目录运行，将结果保存到本地和 W&B。首次使用 W&B 前，执行一次 `wandb login`。

传入 Kustomize 目录即可使用其中的模型服务。单模型部署自动选择模型，多模型部署通过 `--model` 指定。评测已有端点时，将目录换成 `--url`，并提供服务的模型名。

## 测量性能

```bash
foretoken perf examples/quickstart \
  --prompt "用一句话解释什么是 token。" \
  --max-concurrency 4 --num-prompts 20 --max-tokens 128 \
  --output local,wandb
```

汇总结果包括请求成功率、延迟和吞吐量。流式请求还报告首 token 耗时（TTFT）和每输出 token 耗时（TPOT）。

[性能评测示例](docs/perf/README_zh.md)涵盖数据集、多轮对话、请求速率、轨迹回放、参数扫描、SLO 搜索和视频生成。定义与单位见[性能指标](metrics_zh.md)。

## 评测模型质量

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 --output local,wandb
```

该命令对 100 道 GSM8K 数学题评分，输出任务指标和样本数。[质量评测](docs/eval/README_zh.md)介绍 lm-evaluation-harness、EvalScope、原生任务参数、已有服务端点和评分报告。

## 剖析执行过程

在负载运行时采集 CPU/GPU 执行过程，再用 `foretoken profile view` 查看时间线。[性能剖析指南](docs/profile/README_zh.md)提供 PyTorch Profiler、NVIDIA Nsight Systems 和沐曦 mcTracer 的准备、采集与查看方法。

## 查看和保存结果

`perf` 和 `eval` 默认同时输出到终端、本地文件和 W&B。通过 `--output` 选择输出位置：

| 输出选项 | 运行结果 |
| --- | --- |
| 不传 `--output`，或使用 `local,wandb` | 打印结果、保存本地文件并上传 W&B |
| `local` | 打印结果并保存本地文件 |
| `wandb` | 打印结果并上传 W&B |
| `local,quiet` | 保存本地文件，不打印控制台汇总 |
| `local,wandb,quiet` | 保存并上传结果，不打印控制台汇总 |

`quiet` 将准备和执行日志保存到 `run.log`，不打印进度，错误仍会显示。选择 W&B 时，该日志也作为附件上传。

每次运行在 `results/` 下保存到独立目录；`--output-dir` 修改结果父目录。通过 `--wandb-project`、`--wandb-entity`、`--wandb-group` 和 `--wandb-run-name` 组织运行。

延迟和吞吐量图表见[性能结果](docs/perf/wandb_zh.md)，任务得分与框架原始报告见[质量结果](docs/eval/README_zh.md#查看评分)，执行时间线见[查看采集结果](docs/profile/README_zh.md#查看结果)。

# 模型服务评测

[English](README.md) | 简体中文

使用 `foretoken perf` 测量延迟和吞吐量，使用 `foretoken eval` 调用 lm-evaluation-harness 或 EvalScope，为模型回答评分。

## 开始使用

使用 Python 3.11 或更高版本安装性能与质量评测工具：

```bash
pip install foretoken

# 从源码目录安装：
# pip install -e .
```

以下命令在[快速开始](../README_zh.md#快速开始)准备的仓库目录运行。传入 Kustomize 目录时，命令会复用已运行的服务，服务不存在时自动部署；结束后只删除本次评测创建的资源。单模型部署自动选择模型，多模型部署通过 `--model` 指定。

## 测量性能

```bash
foretoken perf examples/quickstart \
  --prompt "用一句话解释什么是 token。" \
  --max-concurrency 4 --num-prompts 20 --max-tokens 128 \
  --output local
```

汇总结果包括请求成功率、延迟和吞吐量。流式请求还报告首 token 耗时（TTFT）和每输出 token 耗时（TPOT）。定义与单位见[性能指标](metrics_zh.md)。

对话数据集的后续轮次默认使用数据集记录的答案作为历史；切换为模型生成的历史或限制轮数，见[本地对话数据](docs/coomon_commands/conversations_zh.md)。

[性能评测示例](docs/examples_zh.md)涵盖数据集、多轮对话、请求速率、轨迹回放、参数扫描、SLO 搜索和视频生成。如需同时采集执行时间线，按[性能剖析指南](../observability/profiling_zh.md)添加 `--profile`。全部性能参数见 `foretoken perf --help`。

## 评测模型质量

### lm-evaluation-harness

先运行一小部分 GSM8K 数学题：

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval \
  --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 \
  --output local
```

默认评测框架是 `lm-eval`。任务名、采样设置、示例数量等评测参数直接采用[上游 CLI 的写法](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/interface.md)。例如，添加 `--num_fewshot 0` 使用零样本提示，添加 `--log_samples` 保存逐题输入和回答。连接信息由 Foretoken 提供，其他 API 选项仍可通过 `--model_args` 设置，例如 `--model_args num_concurrent=4`。

Chat Completions 接口适用于根据生成答案评分的任务。通过候选答案的对数似然评分的任务需要其他模型接口；使用此端点时，选择生成式任务变体。

### EvalScope

```bash
foretoken eval examples/quickstart \
  --evaluator evalscope \
  --model Qwen/Qwen3-0.6B \
  --datasets gsm8k --limit 100 \
  --output local
```

使用 [EvalScope 原生参数](https://evalscope.readthedocs.io/zh-cn/latest/get_started/basic_usage.html)配置评测，例如 `--dataset-args` 和 `--generation-config`。两个框架都可以去掉 `--limit`，运行完整的所选任务。提示词和判分设置由所选框架及任务定义。

## 评测已有服务

将部署目录换成 `--url`，并指定服务提供的模型名。此模式不使用 Kubernetes 资源：

```bash
foretoken eval \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --evaluator lm-eval \
  --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 \
  --output local
```

填写服务实际的 Chat Completions URL；需要认证时添加 `--api-key`。`foretoken perf` 使用相同的 URL 和模型选项。Foretoken Gateway 部署则传入 Kustomize 目录，由命令查找地址并配置路由请求头。

## 查看和保存结果

终端先显示所选模型和评测框架，再列出任务分数，以及框架提供的样本数和标准误差。各子集的详细分数和答案提取方式保留在结果文件与 W&B 分数表中。对比运行时，使用相同的框架、任务配置和样本范围。

两个命令默认同时输出到终端、本地文件和 W&B。使用 W&B 前执行 `wandb login` 完成登录；仅需本地结果时，使用上面示例中的 `--output local`。

| 输出选项 | 运行结果 |
| --- | --- |
| 不传 `--output`，或使用 `local,wandb` | 打印结果、保存本地文件并上传 W&B |
| `local` | 打印结果并保存本地文件 |
| `wandb` | 打印结果并上传 W&B |
| `local,quiet` | 保存本地文件，不显示控制台进度和汇总 |
| `local,wandb,quiet` | 保存并上传结果，不显示控制台进度和汇总 |

本地结果默认保存在 `results/` 下，每次运行使用独立目录；`--output-dir` 修改结果父目录，运行结束后会打印保存位置。质量评测的 `metrics.json` 汇总任务指标，`native/` 保留框架报告及其生成的逐样本记录，`evaluator.log` 保存运行日志。性能结果文件见[性能指标](metrics_zh.md)。

W&B 中的质量评测包含任务指标、分数表，以及保存评测文件的 artifact。通过 `--wandb-project`、`--wandb-entity`、`--wandb-group` 和 `--wandb-run-name` 组织对比。[W&B 性能输出](docs/coomon_commands/wandb_zh.md)介绍 `perf` 的延迟、吞吐量和逐请求视图。

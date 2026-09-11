# 模型服务性能评测

[English](README.md) | 简体中文

使用 `foretoken bench` 测量模型服务的延迟、吞吐量和请求成功情况。模型服务可以来自 Foretoken Kustomize 部署，也可以是已经运行的服务 URL。

被测服务需要提供 OpenAI-compatible Chat Completions API。评测已有模型服务时，请传入完整的 `/v1/chat/completions` URL。

## 安装

需要 Python 3.10 或更高版本。安装命令行工具和评测依赖：

```bash
pip install 'foretoken[bench]'

# 在源码仓库中安装：
# pip install -e '.[bench]'
```

## 完成第一次评测

以下两种模型服务来源只能选择一种。

### Foretoken Kustomize 部署

先安装一次 Foretoken 平台，再评测仓库维护的部署配置：

```bash
foretoken install
foretoken bench examples/quickstart --number 10 --output local
```

模型服务已经运行时，命令会直接复用；尚未部署时，命令会应用 Kustomize 资源、等待服务就绪、完成评测，并且只清理本次创建的资源。Kustomize 评测未指定 `--prompt` 或 `--dataset` 时，默认发送 `Hello`。

部署只包含一个模型时，命令会自动确定模型名称；多模型部署需要添加 `--model MODEL_ID`。

### 已有模型服务

已有服务可以直接评测，不需要安装 Foretoken 平台或准备 Kubernetes：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --prompt "你好" \
  --parallel 2 \
  --number 20 \
  --output local
```

使用 `--url` 时必须同时提供 `--model`，并且不能再传 Kustomize 路径。

## 选择负载

| 目标 | 负载来源 |
| --- | --- |
| 将固定提示词作为单轮对话重复发送 | `--prompt TEXT` |
| 使用本地对话数据 | `--dataset FILE.jsonl` |
| 使用 Hugging Face 数据集 | `--dataset ORG/NAME:SPLIT` |
| 使用 Hugging Face 数据集仓库中的文件 | `--dataset hf://datasets/ORG/NAME@REVISION/PATH` |
| 按 token 长度生成随机提示词 | `--dataset random --tokenizer-path TOKENIZER` |
| 按记录的到达时间回放请求 | `--trace TRACE --dataset DATASET` |
| 汇总多个数据集 | 在 `--dataset` 中用逗号分隔多个选择器 |
| 比较多组负载和生成参数 | 对 Kustomize 部署使用 `--sweep FILE.jsonl` |

每类负载的可复制命令见[评测配方](docs/examples_zh.md)。

## 对话如何执行

所有非轨迹负载统一按对话执行。固定提示词、随机提示词，以及只有一个 `user` 消息的数据行都是单轮对话；本地 JSONL、Hugging Face 和 ShareGPT 数据行可以包含多个轮次。

- 默认 `--max-turns -1`，执行数据行中的全部用户轮次。
- `--max-turns N` 只执行前 `N` 个用户轮次。
- `--number` 表示对话数。使用数据集时，每个选中的数据行是一段对话；`--parallel` 表示并发推进的对话数。
- 数据集中的 `assistant` 消息只作为参考轮次边界；进入下一轮前，其内容会替换为模型的真实回答。

数据行可以使用 OpenAI 风格的 `messages` 字段、`prompt` 或 `user` 字段，也可以使用 ShareGPT `conversations` 格式，其中 `from` 为 `human` 或 `gpt`，正文位于 `value`。

对话数据不支持工具定义、tool call 或 `tool` role 消息。实际包含多轮的对话不能使用正数 `--rate` 或 `--open-loop`。

## 控制请求负载

未设置正数 `--rate` 时，命令会在 `--parallel` 限制内尽快发送请求。

- `--parallel N` 设置并发上限。
- `--number N` 设置对话数；固定提示词和随机提示词各生成单轮对话。
- `--rate R` 按每秒 `R` 个请求的泊松到达过程调度，同时保留并发上限。
- `--open-loop --rate R` 去掉并发上限，保持指定的请求到达率。

`--open-loop` 必须配合正数 `--rate`；不支持无并发上限且按最快速度发送的组合。

## 查找和阅读结果

启用本地结果时，命令会在评测结束后打印结果目录。结果默认位于 `results/<timestamp>/`，可用 `--output-dir PATH` 修改父目录。

先在控制台汇总或 `metrics.json` 中查看以下指标：

- **成功率**：比较性能前，先确认模型服务完成了预期负载。
- **Latency**：请求端到端耗时，p95 和 p99 反映尾部延迟。
- **TTFT**：流式响应从请求发出到首个 token 的时间。
- **TPOT**：流式响应产生首个 token 后，每个输出 token 的平均时间。
- **Generation tokens/s**：模型总输出吞吐量。
- **Generation tokens/s/user**：closed-loop 负载的输出吞吐量除以 `--parallel`。这里的 user 是配置的并发比例，不代表真实用户数；open-loop 负载的分母固定为 1。
- **Requests/s**：完成请求的吞吐量。

参数扫描还会根据所选模型声明的 GPU 容量，报告每张 GPU 的生成吞吐量。

使用对话数据时，请求指标统计实际发送的 HTTP 轮次；对话部分提供对话级延迟和每秒尝试对话数。任一轮失败都会终止当前对话，因此成功轮次数不能解释为成功对话数。

标准负载的逐请求记录保存在 `benchmark_data.db`，失败详情可在 `benchmark.log` 中查看。轨迹回放和参数扫描会在同一结果目录中增加对应汇总。参数扫描至少产生两个有效负载点时，还会生成 `pareto/PARETO.png`，用于比较每个配置用户或并发对话的生成吞吐量与每张 GPU 的生成吞吐量。

## 选择结果去向

默认会打印汇总、保存本地结果，并尝试上传到 Weights & Biases（W&B）。W&B 不可用时，本地结果仍会保留。

| 目标 | 参数 |
| --- | --- |
| 仅保存本地结果 | `--output local` |
| 保存本地结果但不打印控制台汇总 | `--output local,quiet` |
| 保存本地结果并上传 W&B | 不传 `--output`，或使用 `--output local,wandb` |
| 仅上传 W&B | `--output wandb` |

使用 `--wandb-project`、`--wandb-entity` 和 `--wandb-run-name` 指定 W&B 项目、账号与运行名称。

## 必要的组合限制

- 轨迹回放把选中窗口内的每条记录作为独立请求，不会在记录之间延续对话。
- 正数 `--max-turns` 不能与 `--trace` 组合。轨迹的请求数和到达时间由记录决定，并发限制应使用 `--trace-max-concurrency`，而不是 `--parallel`。
- Mooncake 前缀复用回放需要同时使用 `--dataset random`、tokenizer 和 `--trace-synthetic-prefix-reuse`。
- 参数扫描只支持 Foretoken Kustomize 部署，不能与轨迹回放或多个数据集选择器组合。
- `--no-stream` 只报告请求延迟，不报告 TTFT 和 TPOT。

完整参数见 `foretoken bench --help`。

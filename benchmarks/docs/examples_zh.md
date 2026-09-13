# 评测配方

[English](examples.md) | 简体中文

如果已按默认模式部署[快速开始示例](../../examples/quickstart/README_zh.md)，先在仓库根目录获取服务地址：

```bash
MODEL_SERVICE_BASE_URL="$(foretoken endpoint examples/quickstart)"
export MODEL_SERVICE_URL="${MODEL_SERVICE_BASE_URL%/}/v1/chat/completions"
export MODEL_ID=Qwen/Qwen3-0.6B
```

评测其他已有服务时，使用该服务的实际 Chat Completions URL 和模型 ID。也可以把以下命令中的 `--url "$MODEL_SERVICE_URL" --model "$MODEL_ID"` 换成 `examples/quickstart`，由 Foretoken 自动发现服务。Gateway 模式使用这种 Kustomize 写法，路由请求头会自动配置。

示例使用 `--output local,wandb` 保存并上传结果。首次上传前运行 `wandb login`；仅需本地结果时改用 `--output local`。

## 重复发送固定提示词

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --prompt "用一句话解释什么是 token。" \
  --parallel 4 \
  --number 20 \
  --max-tokens 64 \
  --output local,wandb
```

Kustomize 评测未传 `--prompt` 或 `--dataset` 时，默认发送 `Hello`。

## 生成随机提示词

随机负载不需要提前准备数据集，适合控制输入 token 长度。`--tokenizer-path` 可以是本地 tokenizer 目录，也可以是 Hugging Face 模型 ID。

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset random \
  --tokenizer-path "$MODEL_ID" \
  --random-seed 0 \
  --min-prompt-length 128 \
  --max-prompt-length 512 \
  --min-output-length 64 \
  --max-output-length 256 \
  --prefix-length 64 \
  --parallel 4 \
  --number 20 \
  --output local,wandb
```

长度范围默认只计算提示词正文。添加 `--apply-chat-template` 后，随机生成时会计入所选 tokenizer 的对话模板开销。`--prefix-length` 增加共享前缀。服务端可能使用不同模板，最终输入 token 数以评测结果为准。

输出上下界用于为每次请求随机选择目标长度，包含边界值，并覆盖 `--max-tokens`。服务需要支持 `min_tokens`、`ignore_eos` 并返回输出 token 用量；未达到目标长度的请求记为失败。不传这两个参数时，仍按普通生成方式允许提前结束。

## 使用本地 JSONL 数据

先创建包含一个单轮对话和一个多轮对话的数据文件：

```bash
cat > /tmp/foretoken-conversations.jsonl <<'JSONL'
{"messages":[{"role":"user","content":"说出一种三原色。"}]}
{"messages":[{"role":"system","content":"请简短回答。"},{"role":"user","content":"说出一颗行星。"},{"role":"assistant","content":"火星。"},{"role":"user","content":"再说一颗。"},{"role":"assistant","content":"金星。"}]}
JSONL

foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset /tmp/foretoken-conversations.jsonl \
  --number 2 \
  --parallel 2 \
  --output local,wandb
```

默认 `--max-turns -1`，会执行完整对话。第二行的追问会接着模型的第一轮回答继续，而不固定使用数据集里的“火星”。

只执行每行的第一个用户轮次：

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset /tmp/foretoken-conversations.jsonl \
  --max-turns 1 \
  --number 2 \
  --output local,wandb
```

## 使用 Hugging Face 数据集

默认配置只有一个数据划分时，直接写仓库 ID 即可。有多个划分时，用 `:train` 等后缀明确选择；也可以指定只有一个划分的配置名称：

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 \
  --number 20 \
  --output local,wandb
```

也可以直接选择 Hugging Face 数据集仓库中的 JSONL 文件：

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset hf://datasets/ORG/REPOSITORY@REVISION/path/to/data.jsonl \
  --parallel 4 \
  --number 20 \
  --output local,wandb
```

请将 `ORG`、`REPOSITORY`、`REVISION` 和文件路径替换为数据集仓库中的实际值。

## 执行 ShareGPT 多轮对话

ShareGPT 数据使用 `conversations`、`from: human|gpt` 和 `value`。先创建一条本地数据，再执行其中两个用户轮次：

```bash
cat > /tmp/foretoken-sharegpt.jsonl <<'JSONL'
{"conversations":[{"from":"human","value":"说出一颗行星。"},{"from":"gpt","value":"火星。"},{"from":"human","value":"再说一颗。"},{"from":"gpt","value":"金星。"}]}
JSONL

foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset /tmp/foretoken-sharegpt.jsonl \
  --max-turns 2 \
  --parallel 2 \
  --number 1 \
  --output local,wandb
```

下一个 `human` 轮次使用模型的真实回答。对话数据也可以包含 system 消息和模型服务支持的图片。

## 携带工具数据

OpenAI 格式的数据行可以设置 `tools`、`tool_choice` 和 `parallel_tool_calls`。已有的 `assistant.tool_calls` 与对应 `tool` 结果会作为完整历史传入，Foretoken 不重新执行工具。模型新生成的工具调用可以作为最后一轮输出；如果后续轮次需要先执行工具，该对话会停止并报告原因，等待 harness 接入后提供执行能力。流式计时统计 `choices` 非空的分片，包含工具调用分片，不包含仅有用量统计的分片。

## 汇总多个数据集

用逗号分隔的数据集会按顺序执行，并生成一份汇总结果。`--number` 会尽量平均分配；不能整除时，前面的数据集多分配一行。

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --parallel 4 \
  --number 20 \
  --output local,wandb
```

随机提示词不能与其他数据集混用，多数据集也不能与参数扫描组合。

## 设置请求到达率

以下命令按每秒 5 个请求的泊松到达过程发送负载，同时把并发限制为 16：

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --prompt "你好" \
  --rate 5 \
  --parallel 16 \
  --number 100 \
  --output local,wandb
```

使用 `--parallel -1` 去掉并发上限，继续按指定速率发送：

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --prompt "你好" \
  --rate 5 \
  --parallel -1 \
  --number 100 \
  --output local,wandb
```

使用 `--rate -1 --parallel -1` 时，指定数量的请求会尽快全部启动。多轮对话要求 `--rate -1`，此时 `--parallel` 控制同时进行的对话数。

## 回放 StudyChat 轨迹

每条轨迹记录是独立请求。使用时间窗口和 `--trace-max-concurrency` 控制回放，不使用 `--max-turns`、`--parallel`、`--number` 或 `--rate`。

`--trace` 提供请求到达时间，`--dataset` 提供请求内容。两者都选择 StudyChat 时，命令直接使用记录中的请求内容。

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --trace KrisQ/StudyChat \
  --dataset KrisQ/StudyChat \
  --trace-start 600 \
  --trace-duration 300 \
  --trace-max-concurrency 32 \
  --output local,wandb
```

从轨迹开始后的第 600 秒起回放，持续 300 秒。请求保持原始相对到达时间；并发限制导致的等待会计入回放延迟指标。

## 回放 Mooncake 前缀复用

Mooncake 记录请求长度和共享前缀，不包含原始文本。以下示例生成合成提示词，用于评测前缀复用：

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --trace valeriol29/mooncake-traces:conversation \
  --trace-start 2620 \
  --trace-duration 30 \
  --dataset random \
  --tokenizer-path "$MODEL_ID" \
  --random-seed 0 \
  --trace-synthetic-prefix-reuse \
  --trace-max-concurrency 16 \
  --max-tokens 64 \
  --output local,wandb
```

共享前缀按轨迹中的 512-token 块生成。服务端重新分词可能改变块边界，实际缓存命中应结合模型服务的指标确认。

## 扫描评测参数

参数扫描只支持 Foretoken Kustomize 部署，不能同时使用轨迹回放或多个数据集。仓库维护的 `benchmarks/examples/sweep.jsonl` 定义了两组并发负载点：

```jsonl
{"_benchmark_name": "n10", "parallel": [1, 2, 4, 8], "number": 10, "max_tokens": 64}
{"_benchmark_name": "n20", "parallel": [1, 2], "number": 20, "max_tokens": 128}
```

在同一个已部署或临时部署的模型服务上执行全部负载点：

```bash
foretoken bench examples/quickstart \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 \
  --max-prompt-length 512 \
  --sweep benchmarks/examples/sweep.jsonl \
  --experiment-name quickstart-sweep \
  --output local,wandb
```

每个有效负载点都会保存在实验目录中。至少两个负载点成功时，`pareto/PARETO.png` 会比较每个配置用户的生成吞吐量与每张 GPU 的生成吞吐量。

JSONL 每行可以修改以下参数：

- `parallel`、`number` 和 `rate` 等负载字段；
- `max_tokens`、`stream`、采样参数和 `extra_body` 等生成字段；
- `dataset`、`max_turns`、提示词长度、随机种子和偏移等数据字段。

一次实验中的模型服务、凭据、轨迹来源和结果去向保持不变。`parallel`、`number` 或 `rate` 的列表会展开成多个负载点。同一行不能同时扫描 `parallel` 和 `rate`。

## 上传结果到 W&B

指定项目、分组和运行名称：

```bash
wandb login

foretoken bench examples/quickstart \
  --number 20 \
  --wandb-project foretoken-bench \
  --wandb-group qwen-comparison \
  --wandb-run-name quickstart \
  --output local,wandb
```

扫描和多数据集评测在未指定 group 时自动分组，各子运行会在运行名后追加自己的标识。单次评测默认不分组，也可用 `--wandb-group` 指定。

仅保存本地结果用 `--output local`，不打印控制台汇总用 `--output local,quiet`，仅上传用 `--output wandb`。`--wandb-entity` 可指定账号或团队。

## 结果截图

以下小规模运行展示结果样式，命令和请求数量见命令行截图。命令行图片来自实际日志摘录，已隐藏内部地址与路径；W&B 图片对应同次运行。

| 负载 | 命令行 | W&B |
| --- | --- | --- |
| 固定提示词 | [输出](imgs/fixed-prompt-cli.png) | [运行页面](imgs/fixed-prompt-wandb.png) |
| 非流式 | [输出](imgs/nonstream-cli.png) | [运行页面](imgs/nonstream-wandb.png) |
| 本地对话 | [输出](imgs/local-dataset-benchmark-output.png) | [运行页面](imgs/local-dataset-wandb-dashboard.png) |
| 多数据集 | [输出](imgs/multi-dataset-benchmark-output.png) | [第一个数据集](imgs/multi-dataset-first-wandb.png)、[第二个数据集](imgs/multi-dataset-second-wandb.png) |
| 指定到达率 | [输出](imgs/arrival-rate-cli.png) | [运行页面](imgs/arrival-rate-wandb.png) |
| 随机长度 | [输出](imgs/random-dataset-benchmark-output.png) | [运行页面](imgs/random-dataset-wandb-dashboard.png) |
| 本地 StudyChat 格式轨迹 | [输出](imgs/trace-studychat-benchmark-output.png) | [运行页面](imgs/trace-studychat-wandb-dashboard.png) |

## 理解结果

`metrics.json` 保存汇总指标，`raw_output.json` 保存逐请求记录。标准负载还保留 `benchmark_data.db` 和 `benchmark.log`。

| 指标 | 含义 |
| --- | --- |
| Success rate | 成功请求数除以尝试请求数 |
| End-to-end latency (E2EL) | 请求端到端耗时；成功的流式请求计时到最后一个 `choices` 非空的分片 |
| TTFT | 从发送请求到收到首个 `choices` 非空分片的时间 |
| TPOT | `(E2EL − TTFT) / (输出 token 数 − 1)`；输出不足两个 token 时不可用 |
| ITL | 相邻 `choices` 非空分片的到达间隔；一个分片可能包含多个 token |
| Time to final-answer token (TTFAT) | 从整段对话开始到最终回答首个分片的时间 |
| Request throughput (req/s) | 每秒成功完成的请求数 |
| Output token throughput (tokens/s) | 成功请求的输出 token 总数除以运行时间 |
| Output token throughput per user (tokens/s) | 总输出吞吐量除以配置的并发数；`--parallel -1` 时等于总输出吞吐量 |
| Output token throughput per GPU (tokens/s) | 总输出吞吐量除以模型声明的 GPU 容量，用于扫描结果比较 |
| Benchmark duration (s) | 整次评测的持续时间 |

`--no-stream` 保留延迟和吞吐量，不报告 TTFT、TPOT 和 ITL。仅含用量统计的分片不计入流式计时。

多轮数据的请求指标统计实际执行的 HTTP 轮次，某轮失败会终止当前对话。对话指标统计尝试的对话及其耗时，成功轮次数不等于成功对话数。多数据集汇总保留各数据集的对话百分位，不直接平均百分位数。

默认不重试。`--max-retries N` 允许对暂时性故障最多额外尝试 `N` 次，重试耗时计入该次请求延迟。

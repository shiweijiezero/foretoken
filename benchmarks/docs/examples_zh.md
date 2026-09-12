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
  --prefix-length 64 \
  --parallel 4 \
  --number 20 \
  --max-tokens 64 \
  --output local,wandb
```

长度范围默认只计算提示词正文。添加 `--apply-chat-template` 后，随机生成时会计入所选 tokenizer 的对话模板开销。`--prefix-length` 增加共享前缀。服务端可能使用不同模板，最终输入 token 数以评测结果为准。

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

数据集选择器需要在最后一个冒号后写明 split 或 configuration：

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

进入下一个 `human` 轮次前，模型的真实回答会替换参考 `gpt` 内容。对话数据可以包含 system 消息，以及模型服务支持的图片 content 数组等结构化消息内容，但不能包含顶层工具定义、tool call 或 `tool` role 消息。

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

如需 open-loop 负载，可去掉并发上限：

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --prompt "你好" \
  --rate 5 \
  --open-loop \
  --number 100 \
  --output local,wandb
```

实际包含多轮的对话不支持正数 `--rate` 或 `--open-loop`。

## 回放 StudyChat 轨迹

每条轨迹记录是独立请求。使用时间窗口和 `--trace-max-concurrency` 控制回放，不使用 `--max-turns`、`--parallel`、`--number`、`--rate` 或 `--open-loop`。

`--trace` 提供请求到达时间，`--dataset` 提供请求内容。两者都选择 StudyChat 时，命令直接使用记录中的请求内容。

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --trace KrisQ/StudyChat:train \
  --dataset KrisQ/StudyChat:train \
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

- `parallel`、`number`、`rate` 和 `open_loop` 等负载字段；
- `max_tokens`、`stream`、采样参数和 `extra_body` 等生成字段；
- `dataset`、`max_turns`、提示词长度、随机种子和偏移等数据字段。

一次实验中的模型服务、凭据、轨迹来源和结果去向保持不变。`parallel`、`number` 或 `rate` 的列表会展开成多个负载点。同一行不能同时扫描 `parallel` 和 `rate`。

## 上传结果到 W&B

W&B 默认启用。首次使用先登录，再按需指定项目和运行名称：

```bash
wandb login

foretoken bench examples/quickstart \
  --number 20 \
  --wandb-project foretoken-bench \
  --wandb-run-name quickstart \
  --output local,wandb
```

只需要本地结果时使用 `--output local`。结果目录和指标解释见[模型服务性能评测](../README_zh.md)。

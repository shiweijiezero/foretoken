# Benchmarks

[English](README.md) | 简体中文

`benchmarks/` 是 Foretoken 的评测模块。

它可根据 Kustomize 配置发现已部署的 Foretoken 服务，也可连接现有的 OpenAI-compatible endpoint，随后测量性能、比较配置，并检查回答质量是否达标。目标是通过可复现实验回答：“该服务能否满足延迟和吞吐要求，且质量是否足够好？”

## 适用场景

- 了解当前服务在特定并发度或到达率下的延迟和吞吐量。
- 比较不同并发度、请求数量、生成参数或服务端配置的表现。
- 确认模型不仅速度快，而且回答和工具调用均正确。
- 在给定的延迟和吞吐要求下，确定合适的负载点或容量方案。

## 主要功能

| 功能 | 说明 |
|---|---|
| 性能压测 | 对推理服务施加压力，测量延迟、吞吐量、首个 token 时延等指标 |
| 负载扫描 | 对多个并发度、请求数或到达率进行扫描，观察性能变化 |
| 参数扫描 | 组合不同服务端参数和压测参数，批量比较配置 |
| 正确性评测 | 检查回答及工具调用是否正确，而非只关注速度 |
| SLO 评测 | 根据延迟和质量目标进行搜索或仿真，辅助容量规划和扩缩容 |

## 会产出什么

- 控制台中的易读汇总结果
- 本地保存的配置、原始结果和指标，方便事后复查
- 可选的 Weights & Biases（W&B）实验记录与图表，便于跨次运行比较和配置选择

默认显示控制台汇总。使用 `--output local` 保存本地产物，使用 `--output wandb` 发布到 W&B，也可以用逗号组合；加入 `quiet` 可关闭控制台输出。本地文件保存在 `--output-dir` 指定的目录中。

## 示例

评测 Foretoken 的 Kubernetes 部署。若服务已存在则直接复用；若尚未部署，CLI 会部署渲染后的资源，并在评测结束后仅清理本次创建的资源。未指定 `--prompt` 或 `--dataset` 时，使用一个简短的内置提示词：

```bash
foretoken bench --deploy examples/quickstart
```

常用采样参数可直接指定；其他与 OpenAI 兼容的请求字段或后端扩展字段可通过 `--extra-body` 传入：

```bash
foretoken bench --deploy examples/quickstart \
  --temperature 0 \
  --top-p 1 \
  --top-k 0 \
  --extra-body '{"seed":7,"min_tokens":8}'
```

使用固定提示词评测现有 endpoint：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --prompt "hello" \
  --parallel 2 \
  --number 20
```

本地数据集文件：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset foretoken/conversation.jsonl \
  --parallel 4 \
  --number 20 \
  --output local,wandb
```

使用随机生成的提示词进行压测（需指定 tokenizer）：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset random \
  --tokenizer-path Qwen/Qwen3.6-27B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --parallel 4 --number 20 --max-tokens 64 \
  --rate 5 \
  --output local,wandb
```

Hugging Face 数据集 ID（数据行格式：`messages` / `prompt` / `user`[+`system`]）：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 \
  --number 20 \
  --output local,wandb
```

可将多个 JSONL 或 Hugging Face 数据源以逗号分隔。`--number` 是所有数据源的请求总数，按顺序平均分配；不能整除时，前面的数据源各多分配一个请求。各数据源按顺序进行压测，随后合并原始结果并重新计算指标。选择 `wandb` 时，
一次实验对应一个 W&B **group**，每个数据集各自一个 **run**：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset /path/a.jsonl,org/name:train,/path/b.jsonl \
  --parallel 4 \
  --number 30 \
  --output local,wandb
```

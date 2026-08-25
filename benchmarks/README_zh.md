# Benchmarks

[English](README.md) | 简体中文

`benchmarks/` 是 Foretoken 的评测模块。

它可以根据 Kustomize 配置找到已经部署的 Foretoken 服务，也可以连接已有的 OpenAI-compatible endpoint，然后测性能、比配置，并检查回答质量是否达标。目标是用可复现的实验，回答「这个服务能不能稳住延迟和吞吐，质量够不够好」。

## 什么时候需要它

- 想知道当前服务在某个并发或到达率下的延迟和吞吐。
- 想对比不同并发、请求量、生成参数或服务端配置谁更好。
- 想确认模型不只是快，回答和工具调用也对。
- 想在给定延迟/吞吐要求下，找合适的负载点或容量方案。

## 主要功能

| 功能 | 说明 |
|---|---|
| 性能压测 | 对推理服务加压，测量延迟、吞吐、首包时间等 |
| 负载扫描 | 在多个并发、请求数或到达率上扫一遍，看性能怎么变化 |
| 参数扫描 | 组合不同服务端参数和压测参数，批量对比配置 |
| 正确性评测 | 检查回答对不对、工具调用行不行，不只看速度 |
| SLO 评测 | 按延迟和质量目标做搜索或仿真，辅助定容量和扩缩容 |

## 会产出什么

- 控制台可读的汇总结果
- 本地保存的配置、原始结果和指标，方便事后复查
- 可选的 Weights & Biases（W&B）实验记录与图表，方便跨 run 对比和选配置

默认显示控制台汇总。使用 `--output local` 保存本地产物，使用 `--output wandb` 发布到 W&B，也可以用逗号组合；加入 `quiet` 可关闭控制台输出。本地文件写入 `--output-dir`。

## 示例

评测 Foretoken Kubernetes deployment。服务已经存在时直接复用；尚未部署时，CLI 会创建渲染后的资源，并在评测结束后只清理本次创建的资源。未指定 `--prompt` 或 `--dataset` 时，使用一个简短的内置 prompt：

```bash
foretoken bench --deploy examples/quickstart
```

常用采样参数可以直接指定，其他 OpenAI-compatible 或后端扩展请求字段通过 `--extra-body` 传入：

```bash
foretoken bench --deploy examples/quickstart \
  --temperature 0 \
  --top-p 1 \
  --top-k 0 \
  --extra-body '{"seed":7,"min_tokens":8}'
```

使用固定 prompt 评测已有服务：

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

随机数据压测（需指定 tokenizer）：

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

HuggingFace 数据集（行格式：`messages` / `prompt` / `user`[+`system`]）：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 \
  --number 20 \
  --output local,wandb
```

多个 JSONL / HuggingFace 数据源（逗号分隔）。`--number` 是所有数据源的请求总数，按顺序平均分配；不能整除时，前面的数据源各多分配一个请求。各数据源顺序压测，再合并 raw 并重算指标。选择 `wandb` 时，
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

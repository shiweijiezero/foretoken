# 本地对话数据

[English](conversations.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，直接使用仓库中的[对话数据](../../examples/conversations.jsonl)：

```bash
foretoken perf examples/quickstart \
  --dataset benchmarks/examples/conversations.jsonl \
  --num-prompts 3 --max-concurrency 2 --output local,wandb
```

这条命令发送三个请求，完成一个单轮对话和一个两轮对话。同一对话的下一轮会等待上一轮响应结束后再发送。

默认采用 `--conversation-history dataset`：后续请求使用数据集记录的 assistant 答案作为历史，每轮仍请求模型生成新回答并测量性能。添加 `--conversation-history generated`，则将本次实际生成的回答用于后续历史。

`--num-prompts` 限制所有对话合计发送的 HTTP 请求数。若只运行每段对话的首轮：

```bash
foretoken perf examples/quickstart \
  --dataset benchmarks/examples/conversations.jsonl \
  --max-turns 1 --num-prompts 2 --output local,wandb
```

自己的数据每行放一个 JSON 对象，可使用 `messages`、`prompt`，或 `user` 与可选的 `system` 字段。其他格式见 [ShareGPT](sharegpt_zh.md) 和[工具数据](tools_zh.md)。

## 输出示例

以下为已有服务上包含两轮对话的小规模运行：

![命令行输出](../imgs/local-dataset-benchmark-output.png)

![W&B 运行页面](../imgs/local-dataset-wandb-dashboard.png)

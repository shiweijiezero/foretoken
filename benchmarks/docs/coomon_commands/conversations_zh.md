# 本地对话数据

[English](conversations.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，直接使用仓库中的[对话数据](../../examples/conversations.jsonl)：

```bash
foretoken bench examples/quickstart \
  --dataset benchmarks/examples/conversations.jsonl \
  --num-prompts 3 --max-concurrency 2 --output local,wandb
```

文件包含一个单轮对话和一个多轮对话。默认运行全部用户轮次，每轮都会请求模型生成，但后续请求使用数据集答案作为历史（`--conversation-history dataset`）。添加 `--conversation-history generated` 可改用模型实际回答。该选项只改变历史来源，不改变输出长度控制；轨迹回放仍独立发送记录中的请求。

`--num-prompts` 是这些对话共享的 HTTP 请求预算。多轮对话使用所选到达过程启动，并在每次响应后继续依赖轮次，结果会分别报告请求数和对话数。

只运行首个用户轮次：

```bash
foretoken bench examples/quickstart \
  --dataset benchmarks/examples/conversations.jsonl \
  --max-turns 1 --num-prompts 2 --output local,wandb
```

自己的数据每行放一个 JSON 对象，可使用 `messages`、`prompt`，或 `user` 与可选的 `system` 字段。其他格式见 [ShareGPT](sharegpt_zh.md) 和[工具数据](tools_zh.md)。

## 输出示例

以下为已有服务上包含两轮对话的小规模运行：

![命令行输出](../imgs/local-dataset-benchmark-output.png)

![W&B 运行页面](../imgs/local-dataset-wandb-dashboard.png)

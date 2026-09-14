# 工具数据

[English](tools.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，向支持工具调用的模型服务发送仓库中的[工具请求数据](../../examples/tools.jsonl)：

```bash
foretoken bench examples/quickstart \
  --dataset benchmarks/examples/tools.jsonl --number 1 \
  --output local,wandb
```

示例通过 `tools` 提供天气查询函数。数据行还可设置 `tool_choice`、`parallel_tool_calls`，或包含已有的 `assistant.tool_calls` 与对应 `tool` 结果。这些交互作为完整历史传入，Foretoken 不执行工具。

新生成的工具调用可以作为最后一轮输出。如果下一轮需要先执行工具，对话会停止，不复用无关的参考结果。工具执行需要 harness。

流式计时包含 `choices` 非空的工具调用分片，不包含仅有用量统计的分片。

# 工具数据

[English](tools.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，向支持工具调用的模型服务发送仓库中的[工具请求数据](../../examples/tools.jsonl)：

```bash
foretoken perf examples/quickstart \
  --dataset benchmarks/examples/tools.jsonl --num-prompts 1 \
  --output local,wandb
```

示例通过 `tools` 提供天气查询函数。数据行还可设置 `tool_choice`、`parallel_tool_calls`，或包含已有的 `assistant.tool_calls` 与对应 `tool` 结果。这些交互作为完整历史传入，Foretoken 不执行工具。

默认使用数据集历史，后续轮次沿用记录的答案和工具交互，不受本次生成内容影响。使用 `--conversation-history generated` 时，新生成的工具调用可以作为最后一轮输出；如果还有后续轮次需要执行它，对话会停止。

流式计时包含 `choices` 非空的工具调用分片，不包含仅有用量统计的分片。

# 工具数据

[English](tools.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，在请求中携带工具定义：

```bash
cat > /tmp/foretoken-tools.jsonl <<'JSONL'
{"messages":[{"role":"user","content":"巴黎天气怎么样？"}],"tools":[{"type":"function","function":{"name":"get_weather","description":"查询城市天气","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}],"tool_choice":"auto"}
JSONL

foretoken bench examples/quickstart \
  --dataset /tmp/foretoken-tools.jsonl --number 1 \
  --output local,wandb
```

模型服务需要支持工具调用。数据行还可设置 `parallel_tool_calls`，或包含已有的 `assistant.tool_calls` 和对应 `tool` 结果。这些交互作为完整历史传入，Foretoken 不执行工具。

新生成的工具调用可以作为最后一轮输出。如果下一轮需要先执行工具，对话会停止并报告原因，不复用无关的参考结果。工具执行需要 harness。

流式计时包含 `choices` 非空的工具调用分片，不包含仅有用量统计的分片。

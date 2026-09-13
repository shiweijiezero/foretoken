# Tool data

English | [简体中文](tools_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), send a tool definition with a request:

```bash
cat > /tmp/foretoken-tools.jsonl <<'JSONL'
{"messages":[{"role":"user","content":"What is the weather in Paris?"}],"tools":[{"type":"function","function":{"name":"get_weather","description":"Get weather for a city","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}],"tool_choice":"auto"}
JSONL

foretoken bench examples/quickstart \
  --dataset /tmp/foretoken-tools.jsonl --number 1 \
  --output local,wandb
```

The model service must support tool calling. Rows can also set `parallel_tool_calls` and contain recorded `assistant.tool_calls` with matching `tool` results. Those exchanges stay together as input history; Foretoken does not execute tools.

A generated tool call can be the final response. If execution is needed before another turn, the conversation stops with an error rather than reuse an unrelated recorded result. Tool execution requires a harness.

Streaming timing includes chunks with non-empty `choices`, including tool-call chunks, but excludes usage-only chunks.

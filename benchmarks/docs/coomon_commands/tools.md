# Tool data

English | [简体中文](tools_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), send the repository's [tool request sample](../../examples/tools.jsonl) to a model service with tool-calling support:

```bash
foretoken perf examples/quickstart \
  --dataset benchmarks/examples/tools.jsonl --num-prompts 1 \
  --output local,wandb
```

The sample supplies a weather-query function through `tools`. Rows can also set `tool_choice`, `parallel_tool_calls`, and recorded `assistant.tool_calls` with matching `tool` results. Recorded exchanges stay together as input history; Foretoken does not execute tools.

With the default dataset history, later turns follow the recorded answers and tool exchanges, regardless of the generated response. With `--conversation-history generated`, a generated tool call can be the final response; if another turn requires its execution, the conversation stops.

Streaming timing includes chunks with non-empty `choices`, including tool-call chunks, but excludes usage-only chunks.

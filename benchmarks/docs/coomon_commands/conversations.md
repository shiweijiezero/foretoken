# Local conversations

English | [简体中文](conversations_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), run the repository's [conversation dataset](../../examples/conversations.jsonl):

```bash
foretoken bench examples/quickstart \
  --dataset benchmarks/examples/conversations.jsonl \
  --num-prompts 3 --max-concurrency 2 --output local,wandb
```

The file contains one single-turn and one multi-turn conversation. All user turns run by default. Each turn generates a response, but subsequent requests use recorded dataset answers as history (`--conversation-history dataset`). Add `--conversation-history generated` to use the model's responses instead. This choice changes history only, not output-length controls; trace replay sends its recorded requests independently.

`--num-prompts` is the HTTP request budget shared by the selected conversations. Multi-turn conversation starts use the selected arrival process and continue dependent turns after each response; the result reports both request and conversation counts.

To run only the first user turn:

```bash
foretoken bench examples/quickstart \
  --dataset benchmarks/examples/conversations.jsonl \
  --max-turns 1 --num-prompts 2 --output local,wandb
```

For your own data, use one JSON object per line with `messages`, `prompt`, or `user` and an optional `system` field. See [ShareGPT](sharegpt.md) and [tool data](tools.md) for other formats.

## Example output

A short run against an existing service with a two-turn conversation:

![CLI output](../imgs/local-dataset-benchmark-output.png)

![W&B run](../imgs/local-dataset-wandb-dashboard.png)

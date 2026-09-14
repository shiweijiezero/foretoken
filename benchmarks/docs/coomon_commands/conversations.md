# Local conversations

English | [简体中文](conversations_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), run the repository's [conversation dataset](../../examples/conversations.jsonl):

```bash
foretoken bench examples/quickstart \
  --dataset benchmarks/examples/conversations.jsonl \
  --number 2 --parallel 2 --output local,wandb
```

The file contains one single-turn and one multi-turn conversation. All user turns run by default, using the model's actual answers rather than reference text. `--number` counts conversations, not HTTP turns. Multi-turn conversations require `--rate -1`.

To run only the first user turn:

```bash
foretoken bench examples/quickstart \
  --dataset benchmarks/examples/conversations.jsonl \
  --max-turns 1 --number 2 --output local,wandb
```

For your own data, use one JSON object per line with `messages`, `prompt`, or `user` and an optional `system` field. See [ShareGPT](sharegpt.md) and [tool data](tools.md) for other formats.

## Example output

A short run against an existing service with a two-turn conversation:

![CLI output](../imgs/local-dataset-benchmark-output.png)

![W&B run](../imgs/local-dataset-wandb-dashboard.png)

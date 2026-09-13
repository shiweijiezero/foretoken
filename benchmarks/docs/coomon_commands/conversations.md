# Local conversations

English | [简体中文](conversations_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), create one single-turn and one multi-turn conversation:

```bash
cat > /tmp/foretoken-conversations.jsonl <<'JSONL'
{"messages":[{"role":"user","content":"Name a primary color."}]}
{"messages":[{"role":"system","content":"Answer briefly."},{"role":"user","content":"Name a planet."},{"role":"assistant","content":"Mars."},{"role":"user","content":"Name another one."}]}
JSONL

foretoken bench examples/quickstart \
  --dataset /tmp/foretoken-conversations.jsonl \
  --number 2 --parallel 2 --output local,wandb
```

Each row is a conversation. The default `--max-turns -1` runs every user turn, using the model's actual answers rather than the reference text. `--number` counts conversations, not HTTP turns. Multi-turn conversations require `--rate -1`.

To run only the first user turn:

```bash
foretoken bench examples/quickstart \
  --dataset /tmp/foretoken-conversations.jsonl \
  --max-turns 1 --number 2 --output local,wandb
```

Rows also accept `prompt`, or `user` with an optional `system` field. See [ShareGPT](sharegpt.md) and [tool data](tools.md) for other message formats.

## Example output

A short run against an existing service with a two-turn conversation:

![CLI output](../imgs/local-dataset-benchmark-output.png)

![W&B run](../imgs/local-dataset-wandb-dashboard.png)

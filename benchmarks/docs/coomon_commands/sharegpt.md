# ShareGPT conversations

English | [简体中文](sharegpt_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), create and run a ShareGPT conversation:

```bash
cat > /tmp/foretoken-sharegpt.jsonl <<'JSONL'
{"conversations":[{"from":"human","value":"Name a planet."},{"from":"gpt","value":"Mars."},{"from":"human","value":"Name another one."}]}
JSONL

foretoken bench examples/quickstart \
  --dataset /tmp/foretoken-sharegpt.jsonl \
  --max-turns 2 --number 1 --output local,wandb
```

`human` identifies a user message and `gpt` a reference assistant answer. The follow-up uses the model's actual answer. To include system messages or image content, use the OpenAI-style `messages` format in the [local dataset guide](conversations.md).

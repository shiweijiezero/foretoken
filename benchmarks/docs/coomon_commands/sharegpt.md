# ShareGPT conversations

English | [简体中文](sharegpt_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), run the repository's [ShareGPT sample](../../examples/sharegpt.jsonl):

```bash
foretoken bench examples/quickstart \
  --dataset benchmarks/examples/sharegpt.jsonl \
  --max-turns 2 --number 1 --output local,wandb
```

`human` identifies a user message and `gpt` a reference answer. The follow-up uses the model's actual answer. For system messages or image content, use the OpenAI-style `messages` format in the [local dataset guide](conversations.md).

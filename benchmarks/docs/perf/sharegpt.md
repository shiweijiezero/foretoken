# ShareGPT conversations

English | [简体中文](sharegpt_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), run the repository's [ShareGPT sample](../../examples/sharegpt.jsonl):

```bash
foretoken perf examples/quickstart \
  --dataset benchmarks/examples/sharegpt.jsonl \
  --max-turns 2 --num-prompts 2 --output local,wandb
```

`human` identifies a user message and `gpt` the recorded assistant answer. Conversation history options and the OpenAI-style `messages` format for system messages or images are described in [Local conversations](conversations.md).

# ShareGPT conversations

English | [简体中文](sharegpt_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), run the repository's [ShareGPT sample](../../examples/sharegpt.jsonl):

```bash
foretoken perf examples/quickstart \
  --dataset benchmarks/examples/sharegpt.jsonl \
  --max-turns 2 --num-prompts 2 --output local,wandb
```

`human` identifies a user message and `gpt` the recorded assistant answer. The benchmark runs the recorded conversation turn by turn, generating each answer to the token length of its recorded text reference when available. The [ShareGPT SLO sweep](slo.md#measure-attainment-at-fixed-conversation-rates) downloads the original dataset automatically and varies conversation arrival rates. Conversation history options and the OpenAI-style `messages` format for system messages or images are described in [Local conversations](conversations.md).

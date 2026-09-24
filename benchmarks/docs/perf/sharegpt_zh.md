# ShareGPT 对话

[English](sharegpt.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，运行仓库中的 [ShareGPT 数据](../../examples/sharegpt.jsonl)：

```bash
foretoken perf examples/quickstart \
  --dataset benchmarks/examples/sharegpt.jsonl \
  --max-turns 2 --num-prompts 2 --output local,wandb
```

`human` 表示用户消息，`gpt` 表示记录的 assistant 答案。历史来源的选择，以及包含 system 消息或图片的 OpenAI `messages` 格式，见[本地对话数据](conversations_zh.md)。

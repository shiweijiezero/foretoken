# ShareGPT 对话

[English](sharegpt.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，创建并运行 ShareGPT 对话：

```bash
cat > /tmp/foretoken-sharegpt.jsonl <<'JSONL'
{"conversations":[{"from":"human","value":"说出一颗行星。"},{"from":"gpt","value":"火星。"},{"from":"human","value":"再说一颗。"}]}
JSONL

foretoken bench examples/quickstart \
  --dataset /tmp/foretoken-sharegpt.jsonl \
  --max-turns 2 --number 1 --output local,wandb
```

`human` 是用户消息，`gpt` 是参考回答，后续追问使用模型真实生成的回答。需要 system 消息或图片内容时，使用[本地数据指南](conversations_zh.md)中的 OpenAI `messages` 格式。

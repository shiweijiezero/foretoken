# 本地对话数据

[English](conversations.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，创建一个单轮对话和一个多轮对话：

```bash
cat > /tmp/foretoken-conversations.jsonl <<'JSONL'
{"messages":[{"role":"user","content":"说出一种三原色。"}]}
{"messages":[{"role":"system","content":"请简短回答。"},{"role":"user","content":"说出一颗行星。"},{"role":"assistant","content":"火星。"},{"role":"user","content":"再说一颗。"}]}
JSONL

foretoken bench examples/quickstart \
  --dataset /tmp/foretoken-conversations.jsonl \
  --number 2 --parallel 2 --output local,wandb
```

每行是一段对话。默认 `--max-turns -1` 执行全部用户轮次，并使用模型真实回答继续，而不是参考答案。`--number` 统计对话数，不是 HTTP 轮次数。多轮要求 `--rate -1`。

只运行首个用户轮次：

```bash
foretoken bench examples/quickstart \
  --dataset /tmp/foretoken-conversations.jsonl \
  --max-turns 1 --number 2 --output local,wandb
```

数据行也接受 `prompt`，或 `user` 与可选的 `system` 字段。其他格式见 [ShareGPT](sharegpt_zh.md) 和[工具数据](tools_zh.md)。

## 输出示例

以下为已有服务上包含两轮对话的小规模运行：

![命令行输出](../imgs/local-dataset-benchmark-output.png)

![W&B 运行页面](../imgs/local-dataset-wandb-dashboard.png)

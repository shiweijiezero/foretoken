# 非流式请求

[English](non-streaming.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，在仓库根目录运行：

```bash
foretoken bench examples/quickstart \
  --prompt "说出一颗行星。" --no-stream \
  --number 20 --max-tokens 32 \
  --output local,wandb
```

服务一次返回完整回答。仍统计端到端耗时和吞吐量，不报告 TTFT、TPOT 和 ITL。

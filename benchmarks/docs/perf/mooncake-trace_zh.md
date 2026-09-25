# Mooncake trace 回放

[English](mooncake-trace.md) | 简体中文 · [性能评测示例](README_zh.md)

Mooncake trace 数据集记录请求长度和共享前缀块，不包含原始文本。完成[准备步骤](README_zh.md#准备)后，按记录生成共享前缀输入：

```bash
foretoken perf examples/quickstart \
  --trace valeriol29/mooncake-traces:conversation \
  --trace-start 57 --trace-duration 5 \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --random-seed 0 --trace-synthetic-prefix-reuse \
  --trace-max-concurrency 16 --max-tokens 64 \
  --output local,wandb
```

输入复用轨迹中的 512-token 块。服务端重新分词可能改变边界，实际缓存命中以服务指标为准。此模式不与 `--prefix-length` 组合。

去掉 `--trace-synthetic-prefix-reuse` 后，按记录长度生成普通随机输入，不重建共享块。若轨迹行包含正整数 `output_length`，无论输入来自随机生成、共享前缀重建还是独立数据集，请求都会以该值为精确输出目标；原始结果记录目标和实际输出数。时间窗口和并发规则见 [StudyChat 回放](studychat_zh.md)。

![同一 W&B group 中的普通随机输入与前缀重建对比](../imgs/mooncake-wandb.png)

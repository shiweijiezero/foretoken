# 随机负载

[English](random.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，生成随机输入，并为每次请求抽取目标输出长度：

```bash
foretoken perf examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --min-output-length 64 --max-output-length 256 \
  --prefix-length 64 --random-seed 0 \
  --max-concurrency 4 --num-prompts 20 --output local,wandb
```

输入长度默认只计算正文。`--apply-chat-template` 计入所选 tokenizer 的模板开销，服务端可能使用不同模板。`--prefix-length` 增加共享前缀。tokenizer 也可以使用本地目录。

输出范围包含上下界，并覆盖 `--max-tokens`。服务需要支持 `min_tokens`、`ignore_eos` 并返回输出用量；未达到目标长度的请求记为失败。不传这两个参数时，普通生成允许提前结束。

## 输出示例

以下运行使用较小的长度范围：

![命令行输出](../imgs/random-dataset-benchmark-output.png)

![W&B 运行页面](../imgs/random-dataset-wandb-dashboard.png)

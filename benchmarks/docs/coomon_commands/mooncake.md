# Mooncake prefix reuse

English | [简体中文](mooncake_zh.md) · [Common commands](../examples.md)

Mooncake records request lengths and shared prefix blocks, not the original text. After [setup](../examples.md#setup), generate synthetic inputs with those shared prefixes:

```bash
foretoken bench examples/quickstart \
  --trace valeriol29/mooncake-traces:conversation \
  --trace-start 2620 --trace-duration 30 \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --random-seed 0 --trace-synthetic-prefix-reuse \
  --trace-max-concurrency 16 --max-tokens 64 \
  --output local,wandb
```

Inputs reuse the trace's 512-token blocks. Server-side tokenization may change those boundaries; inspect service metrics for actual cache hits. Do not combine this mode with `--prefix-length`.

Omit `--trace-synthetic-prefix-reuse` to generate random inputs from the recorded lengths without shared-block reconstruction. General trace-window and concurrency rules are described in [StudyChat replay](studychat.md).

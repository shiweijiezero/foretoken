# Mooncake trace replay

English | [简体中文](mooncake-trace_zh.md) · [Performance examples](README.md)

The Mooncake trace dataset records request lengths and shared prefix blocks, not the original text. After [setup](README.md#setup), generate synthetic inputs with those shared prefixes:

```bash
foretoken perf examples/quickstart \
  --trace valeriol29/mooncake-traces:conversation \
  --trace-start 57 --trace-duration 5 \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --random-seed 0 --trace-synthetic-prefix-reuse \
  --trace-max-concurrency 16 --max-tokens 64 \
  --output local,wandb
```

Inputs reuse the trace's 512-token blocks. Server-side tokenization may change those boundaries; inspect service metrics for actual cache hits. Do not combine this mode with `--prefix-length`.

Omit `--trace-synthetic-prefix-reuse` to generate random inputs from the recorded lengths without shared-block reconstruction. When a trace row has positive integer `output_length`, the request targets that exact output count, whether the payload comes from random generation, reconstructed prefixes, or a separate dataset. The recorded target and actual count appear in raw results. General trace-window and concurrency rules are described in [StudyChat replay](studychat.md).

![Random inputs and reconstructed prefixes in the same W&B group](../imgs/mooncake-wandb.png)

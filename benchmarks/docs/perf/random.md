# Random workloads

English | [简体中文](random_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), generate random inputs and sample an output target for each request:

```bash
foretoken perf examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --min-output-length 64 --max-output-length 256 \
  --prefix-length 64 --random-seed 0 \
  --max-concurrency 4 --num-prompts 20 --output local,wandb
```

The input range covers prompt content. `--apply-chat-template` accounts for the selected tokenizer's template overhead; the service may use a different template. `--prefix-length` adds a shared prefix. A local tokenizer directory can replace the model ID.

Both output bounds are inclusive and override `--max-tokens`. The service must support `min_tokens` and `ignore_eos` and report output usage. A request that misses its target counts as failed. Omit both bounds for ordinary generation that can end early.

## Example output

A short run with smaller length bounds:

![CLI output](../imgs/random-dataset-benchmark-output.png)

![W&B run](../imgs/random-dataset-wandb-dashboard.png)

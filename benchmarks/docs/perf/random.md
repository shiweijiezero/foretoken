# Random workloads

English | [简体中文](random_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), generate random inputs and sample an output target for each request:

```bash
foretoken perf examples/quickstart \
  --dataset random \
  --min-prompt-length 128 --max-prompt-length 512 \
  --min-output-length 64 --max-output-length 256 \
  --prefix-length 64 --random-seed 0 \
  --max-concurrency 4 --num-prompts 20 --output local,wandb
```

Random workloads use the selected tokenizer to generate prompts with the requested lengths. `--prefix-length` adds a shared prefix, and `--apply-chat-template` is not supported. The tokenizer is inferred from the model service; use `--tokenizer-path` when the service model name is an alias or its tokenizer files are not available locally.

Both output bounds are inclusive and override `--max-tokens`. The service must support `min_tokens` and `ignore_eos` and report output usage. A request that misses its target counts as failed. Omit both bounds for ordinary generation that can end early.

## Example output

A short run with smaller length bounds:

![CLI output](../imgs/random-dataset-benchmark-output.png)

![W&B run](../imgs/random-dataset-wandb-dashboard.png)

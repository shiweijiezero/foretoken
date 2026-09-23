# Multiple datasets

English | [简体中文](multi-dataset_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), separate dataset selectors with commas:

```bash
foretoken perf examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --max-concurrency 4 --num-prompts 20 --output local,wandb
```

Datasets share one arrival clock, concurrency limit, and request budget, and the result retains each request's dataset identity. `--num-prompts` is divided as evenly as possible, with any remainder assigned to the earlier datasets. Local JSONL paths can replace the remote selectors. Random inputs cannot be mixed with other datasets.

Multi-dataset workloads can be included in HTTP sweeps and SLO searches. Dataset-level summaries remain separate; conversation percentiles are not averaged across datasets.

## Example output

A short run over two local datasets:

![Combined CLI output](../imgs/multi-dataset-benchmark-output.png)

![Dataset curves compared in one W&B group](../imgs/multi-dataset-wandb.png)

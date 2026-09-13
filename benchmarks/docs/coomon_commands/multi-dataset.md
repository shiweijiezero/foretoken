# Multiple datasets

English | [简体中文](multi-dataset_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), separate dataset selectors with commas:

```bash
foretoken bench examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --parallel 4 --number 20 --output local,wandb
```

Datasets run in order and produce one combined result. `--number` is divided as evenly as possible, with any remainder assigned to the earlier datasets. Local JSONL paths can replace the remote selectors. Random inputs cannot be mixed with other datasets, and multi-dataset runs cannot use `--sweep`.

Each dataset gets its own W&B run in the same group. Conversation percentiles remain per dataset rather than being averaged.

## Example output

A short run over two local datasets:

![Combined CLI output](../imgs/multi-dataset-benchmark-output.png)

![Dataset curves compared in one W&B group](../imgs/multi-dataset-wandb.png)

# Hugging Face datasets

English | [简体中文](huggingface_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), select a dataset and split:

```bash
foretoken bench examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 --number 20 --output local,wandb
```

The suffix can be omitted when the repository's default configuration has one split. If a choice is needed, specify `:train` or another split. A configuration name is also accepted when that configuration has a single split.

For a JSONL file in a dataset repository, replace the organization, repository, revision, and path below with the file's actual location:

```bash
foretoken bench examples/quickstart \
  --dataset hf://datasets/ORG/REPOSITORY@REVISION/path/to/data.jsonl \
  --parallel 4 --number 20 --output local,wandb
```

Rows use the same [conversation formats](conversations.md) as local data. `--dataset-offset` skips rows before selecting the requested count.

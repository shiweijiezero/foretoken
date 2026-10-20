# Hugging Face datasets

English | [简体中文](huggingface_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), use the public StudyChat dataset:

```bash
foretoken perf examples/quickstart/qwen3-0.6b \
  --dataset KrisQ/StudyChat \
  --max-concurrency 2 --num-prompts 2 --output local,wandb
```

The repository's default configuration has one split, so no suffix is needed. For datasets with multiple splits, append the split name, such as `:train`.

To select the repository's [JSONL file](https://huggingface.co/datasets/KrisQ/StudyChat/blob/main/data.jsonl) directly:

```bash
foretoken perf examples/quickstart/qwen3-0.6b \
  --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
  --max-concurrency 2 --num-prompts 2 --output local,wandb
```

File URIs require the full dataset repository ID, including its user or organization (`org/name`). Append `@revision` after the repository ID to select a branch, tag, or pull-request revision such as `@refs/pr/10`; encode `/` in other branch names as `%2F`.

File selection downloads the file into the Hugging Face cache. Rows use the same [conversation formats](conversations.md) as local data. Add `--max-turns 1` for first-turn-only evaluation, or `--dataset-offset` to skip initial rows.

![Twenty StudyChat requests in send order](../imgs/huggingface-wandb.png)

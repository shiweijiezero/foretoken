# Hugging Face 数据集

[English](huggingface.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，直接使用公开的 StudyChat 数据集：

```bash
foretoken perf examples/quickstart \
  --dataset KrisQ/StudyChat \
  --max-concurrency 2 --num-prompts 2 --output local,wandb
```

该仓库的默认配置只有一个数据划分，无需后缀。有多个划分的数据集可添加 `:train` 等名称选择。

也可以直接指定仓库里的 [JSONL 文件](https://huggingface.co/datasets/KrisQ/StudyChat/blob/main/data.jsonl)：

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
  --max-concurrency 2 --num-prompts 2 --output local,wandb
```

直接指定文件时会将文件下载到 Hugging Face 缓存。数据行使用与[本地对话数据](conversations_zh.md)相同的格式。添加 `--max-turns 1` 可只评测首轮，`--dataset-offset` 可跳过开头的数据行。

![按发送顺序展示的 20 个 StudyChat 请求](../imgs/huggingface-wandb.png)

# Hugging Face 数据集

[English](huggingface.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，选择数据集及数据划分：

```bash
foretoken bench examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 --number 20 --output local,wandb
```

默认配置只有一个数据划分时可以省略后缀。需要选择时添加 `:train` 等划分名称；也可以指定只有一个划分的配置名称。

使用仓库中的 JSONL 文件时，将下面的组织、仓库、版本和路径替换为文件实际位置：

```bash
foretoken bench examples/quickstart \
  --dataset hf://datasets/ORG/REPOSITORY@REVISION/path/to/data.jsonl \
  --parallel 4 --number 20 --output local,wandb
```

数据行采用与本地数据相同的[对话格式](conversations_zh.md)。`--dataset-offset` 可跳过开头的数据行，再选取指定数量。

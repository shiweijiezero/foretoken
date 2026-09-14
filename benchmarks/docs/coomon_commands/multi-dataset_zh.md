# 多数据集

[English](multi-dataset.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，用逗号分隔数据集：

```bash
foretoken bench examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --parallel 4 --number 20 --output local,wandb
```

数据集按顺序执行，生成一份汇总结果。`--number` 尽量平均分配，余数分给前面的数据集。远程选择器也可换成本地 JSONL 路径。随机输入不能与其他数据集混用，多数据集不支持 `--sweep`。

每个数据集在同一 W&B group 中单独记录。对话百分位保留在各数据集结果中，不直接平均。

## 输出示例

以下为两个本地数据集的小规模运行：

![命令行汇总](../imgs/multi-dataset-benchmark-output.png)

![同一 W&B group 中的数据集曲线对比](../imgs/multi-dataset-wandb.png)

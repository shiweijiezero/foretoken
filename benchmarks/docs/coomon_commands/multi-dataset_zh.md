# 多数据集

[English](multi-dataset.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，用逗号分隔数据集：

```bash
foretoken perf examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --max-concurrency 4 --num-prompts 20 --output local,wandb
```

多个数据集共享同一个到达时钟、并发限制和请求预算，结果保留每条请求的数据集身份。`--num-prompts` 尽量平均分配，余数分给前面的数据集。远程选择器也可换成本地 JSONL 路径。随机输入不能与其他数据集混用。

多数据集负载可以加入 HTTP 参数扫描和 SLO 搜索。数据集级汇总保持分开，对话百分位不跨数据集直接平均。

## 输出示例

以下为两个本地数据集的小规模运行：

![命令行汇总](../imgs/multi-dataset-benchmark-output.png)

![同一 W&B group 中的数据集曲线对比](../imgs/multi-dataset-wandb.png)

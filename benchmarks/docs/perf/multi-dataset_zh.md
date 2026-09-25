# 多数据集

[English](multi-dataset.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，用逗号分隔数据集：

```bash
foretoken perf examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --max-concurrency 4 --num-prompts 20 --output local,wandb
```

多个数据集共享到达时钟、并发限制和请求预算。默认平均分配 `--num-prompts`；两个来源传入 `--dataset-weights 3,1` 时，第一个获得四分之三的配额。仅指定 `--duration` 而不指定 `--num-prompts` 时，按权重抽取对话，再执行选定的轮次。每条请求保留来源身份；有请求配额时，最后一条多轮对话会按该来源的剩余配额截断。远程选择器也可换成本地 JSONL 路径；随机输入不能与其他数据集混用。

JSONL 和 Hugging Face 数据行可以指定 `model`、整数 `priority`、评测标签 `request_class` 和正整数 `output_length`。例如 `{"prompt":"你好","model":"Qwen/Qwen3-0.6B","request_class":"interactive","output_length":32}` 会选择该模型，通过 `min_tokens` 和 `ignore_eos` 请求恰好输出 32 个 token；服务返回的 token 数不符时记为请求失败。省略 `model` 则沿用服务选择；URL 或多模型部署的数据行都指定模型时，无需额外传 `--model`。`priority` 会传给服务端，需要服务支持优先级调度；`request_class` 是结果分组标签。

终端、`metrics.json` 和 W&B 分别展示数据集、模型和请求类别汇总；逐请求标签及目标/实际输出长度保存在 `raw_output.json` 和 W&B 请求表中。各分组吞吐与 goodput 均以整个实验时长为分母。多数据集负载也可用于 HTTP 参数扫描和 SLO 搜索。比较同一混合负载的不同 SLO 目标时，在 `--slo-params` 中传入多个条件对象；每个对象分别对整个负载执行搜索。

## 输出示例

以下为两个本地数据集的小规模运行：

![命令行汇总](../imgs/multi-dataset-benchmark-output.png)

![同一 W&B group 中的数据集曲线对比](../imgs/multi-dataset-wandb.png)

下面是对已有服务发送 3:1 交互式/批量负载时，按请求类别展示的 p95 端到端耗时：

![各请求类别的 p95 端到端耗时](../imgs/mixed-workload-wandb.png)

同一负载按数据行分别请求 16 或 32 个输出 token，目标与实际数量按发送顺序逐条对应：

![W&B 中逐请求的目标和实际输出长度](../imgs/output-length-wandb.png)

# 参数扫描

[English](sweep.md) | 简体中文 · [性能评测示例](README_zh.md)

使用现有[参数文件](../../examples/sweep.jsonl)比较不同并发，并导出图表：

```bash
foretoken perf examples/quickstart \
  --dataset random --min-prompt-length 128 --max-prompt-length 256 \
  --temperature 0 --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 \
  --experiment-name concurrency --output local,wandb,plot
```

该示例在 1、2、4 并发下各发送 384 个请求，每个请求要求输出 256 个 token。每个参数点重复三次，每次重复前预热 16 段对话。也可用 `--url` 和 `--model` 指定已有服务；多轮、多数据集、轨迹和 profile 的用法与单次评测相同。

## 成组设置参数

常用实验配置集中在 [`scripts/common/`](../../scripts/common/)。[固定长度配置](../../scripts/common/fixed-length.jsonl)用不同行保持三组输入／输出长度配对，再展开各行的并发列表。

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/fixed-length.jsonl --num-runs 3 --num-prompts 1000 \
  --warmup-requests 20 --temperature 0 --output local,wandb,plot
```

共 9 个参数点、27 次测量。并发 1 的结果同时用于单请求比较，延迟、吞吐与资源图表复用这些运行。tokenizer 从所选模型服务推导，需要覆盖时才传 `--tokenizer-path`。

负载、生成和数据集字段沿用 CLI 名称，将连字符换成下划线。例如 `request_rate: [4, 8, 16]` 扫描到达率。[固定到达率配置](../../scripts/common/fixed-arrival.jsonl)和[容量配置](../../scripts/common/fixed-capacity.jsonl)可以直接使用。列表表示扫描维度；每次运行混合两个数据集时写作 `"dataset": [["first.jsonl", "second.jsonl"]]`。通过 `--slo-params` 添加 SLO 条件后，`--num-runs` 控制每个点的完整搜索重复次数，每次搜索的探测点测量一轮。

## 比较多种方法

单次负载也可以直接传入多个 Kustomize 示例：

```bash
foretoken perf examples/quickstart examples/quickstart3 \
  --dataset random --num-prompts 100 --output local,wandb,plot
```

多个 endpoint 使用一个 `--url` 后跟多个 URL；`--model` 可以提供一个共享模型名，也可以逐个提供模型名。可复用的 sweep 文件则用 `service` 列表表达相同选择；Kustomize 示例可以直接填写路径。[量化模型 sweep 配置](../../scripts/common/quantized-models.jsonl)用于比较[BF16 和 4-bit 部署](../../../examples/quantized-model/README_zh.md)：

```bash
foretoken perf --dataset random --sweep benchmarks/scripts/common/quantized-models.jsonl \
  --num-prompts 100 --warmup-requests 10 --num-runs 3 \
  --temperature 0 --experiment-name methods --output local,wandb,plot
```

服务路径相对于仓库根目录解析。已有端点使用 `name`、`url`、`model`，可选 `health_url`；认证使用命令的 `--api-key`。

一个方法的全部参数点执行完毕后，再运行下一方法。临时部署在方法切换时删除；已有服务原样复用。修改已有服务的配置后，先用 `foretoken deploy` 应用，再评测。

## 查看结果与重新绘图

结果目录包含 `sweep_summary.csv`、各次运行目录，以及 `plots/` 下的 PDF、SVG、PNG 和 CSV 文件。统计保留每个指标的有效样本数；误差线表示各轮结果的样本标准差，分位数指标也先逐轮计算、再汇总。只有一轮时不估计误差。

W&B 将各次运行放在同一 group，并添加使用相同汇总数据与曲线的对比运行。选择 `plot` 时，也会上传导出的图表文件。重复使用同一个 `--experiment-name` 会覆盖该实验目录；省略它时会创建带时间戳的新目录。

将第一个示例重新绘制为论文双栏宽度，无需发送请求：

```bash
foretoken plot results/concurrency --columns 2
```

用 `--metric` 选择汇总指标，用 `--method` 选择方法，两者均可重复指定。`--output-dir` 将另一版排版保存到独立目录。输出位置见[结果设置](../../README_zh.md#查看和保存结果)。

## 视频参数

```bash
foretoken perf video \
  --url http://127.0.0.1:8091/v1/videos/sync \
  --dataset VideoArgusBench/TI2V \
  --sweep benchmarks/examples/video-sweep.jsonl \
  --num-runs 2 --output local,wandb,plot
```

视频扫描点可以改变 `width`、`height`、`num_frames`、`fps`、`num_inference_steps`、`aspect_ratio`、`flow_shift`、`audio_flow_shift`、`seed`、`max_concurrency`、`duration` 和 `warmup_requests`。各点分别保留生成的视频与性能指标。

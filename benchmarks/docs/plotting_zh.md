<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 绘制已保存的评测结果

[English](plotting.md) | 简体中文 · [评测与性能分析](../README_zh.md)

`foretoken plot` 将已保存的测量结果绘制为性能曲线、方案对比图和收益图。在保存结果的机器上运行，并将下方示例路径换成实际结果目录或实验目录。

```bash
foretoken plot results/baseline
```

命令向 `results/baseline/plots/` 导出 PDF、SVG、PNG 和 CSV，也接受包含多次运行的目录。传入多个目录或使用 `--base` 时，默认输出到 `./plots`；用 `--output-dir` 指定其他位置。

## 比较性能

对照两个方案：

```bash
foretoken plot results/baseline results/candidate
```

性能对照包含每用户输出速度与每 GPU 输出吞吐量的权衡图。不同方案使用各自的标记，`c1`、`c16` 等标签表示配置的并发限额。虚线连接帕累托最优点：其中任一坐标继续改善时，另一个坐标就会变差。图中保留所有实测点。

默认沿 `max_concurrency` 扫描，其余条件保持固定；输入长度、输出上限、数据集等条件不同的测量分别绘图。请求速率扫描可使用 `--sweep-axis request_rate --label-by request_rate`。`--label-by` 可选多个已保存的负载字段，`--label-values` 添加坐标，`--columns 2` 增加图宽。

### 选择坐标轴

以 P90 解码速度为横轴、每 GPU 总 token 吞吐量为纵轴：

```bash
foretoken plot results/baseline results/candidate \
  --x-metric decode_interactivity_p90 \
  --y-metric total_tokens_per_second_per_gpu \
  --frontier max-max --label-values --columns 2
```

`--x-metric` 和 `--y-metric` 接受已保存的标量指标名。自选坐标轴默认按扫描顺序连接各点；用 `--frontier` 指定横纵轴的优化方向，可选 `max-max`、`min-max`、`max-min` 或 `min-min`。`--frontier none` 连接全部扫描点。

P90 解码速度基于成功请求的 TPOT，总吞吐量中的缓存输入只计一次。定义和所需数据见[绘图派生指标](metrics_zh.md#绘图派生指标)。结果缺少 GPU 容量时，用 `--gpu-count N` 提供实际数量；它必须与任何已记录容量一致。

### 标出相对基准的变化

```bash
foretoken plot results/candidate --base results/baseline \
  --label-values --columns 2
```

`--base` 读取一个参考结果或迭代目录，包含其中的重复测量。图上连接负载条件相同的点，默认标出横轴指标的变化百分比；用 `--compare-axis y` 改为纵轴。计算方式为 `(candidate / baseline - 1) × 100%`；延迟降低、吞吐量提高表示改善。

## 估算收入与利润

复制[价格示例](../examples/pricing.yaml)，将示例金额换成实际 token 售价和 GPU 每小时成本：

```yaml
currency: USD
input_per_million: 0.15
cached_input_per_million: 0.003
output_per_million: 0.60
gpu_hour_cost: 5.0
```

`input_per_million` 是每百万非缓存输入 token 的价格。各方案成本不同时，将 `gpu_hour_cost` 的数值换成映射，名称与对照 CSV 中的方案名一致：

```yaml
gpu_hour_cost:
  baseline: 5.0
  candidate: 6.0
```

将修改后的文件保存为 `pricing.yaml`，再运行：

```bash
foretoken plot results/baseline results/candidate \
  --pricing pricing.yaml --min-interactivity 200 --columns 2
```

命令在每组固定负载下，为各方案选择平均 P90 解码速度达到 200 tokens/s/user、且平均收入最高的实测点。不加 `--min-interactivity` 则考虑全部测量点。选点只使用实测数据，不插值；收入和利润先按每次运行计算，再汇总重复测量。

收益估算需要所有成功请求的输入、输出和缓存输入用量，以及 GPU 数量。缺失的缓存用量保持不可用。柱状图表示算力成本、盈利或亏损，并标出总收入；CSV 保留每个点的条件满足情况和选择状态。

## 阅读与导出结果

| 输出 | 内容 |
| --- | --- |
| `tradeoff-*.csv` | 图中坐标、配对样本数、标准差及可用的基准变化 |
| `economics-*.csv` | 价格、每 GPU 每小时的估算值、重复次数及运行点选择 |
| `comparison-summary.csv` | 各方案、各负载的标量指标统计 |
| 使用 `--base` 时的 `comparison-base.csv` | 基准与候选的数值、相对变化、失败情况、条件差异和结果路径 |

误差条表示重复运行的样本标准差，只有一次测量时不提供。权衡图的两个轴，以及带速度下限的收益选点，使用同一组可用的重复运行；标量表分别统计各指标的可用样本。指标缺失或基准为零时，对应比较保持不可用，CSV 中标明状态。

用图例中的名称通过 `--method` 选择方案，显式指定的 `--base` 始终保留。用 `--metric generation_tokens_per_second ttft_p95_seconds` 选择普通指标图。显式指定权衡图选项或 `--pricing` 后，命令导出所选图形和表格；可加 `--metric` 同时生成普通指标图。评测分数见[质量结果](eval/README_zh.md#查看评分)。

添加 `--sync_wandb`，可将已有结果及单次运行图表上传到原来的 W&B 运行，跨运行对照图保留在本地。恢复和发布行为见 [W&B 结果补传](perf/wandb_zh.md#恢复上传)。

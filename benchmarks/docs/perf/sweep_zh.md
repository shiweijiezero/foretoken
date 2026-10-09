# 参数扫描

[English](sweep.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，使用现有[参数文件](../../examples/sweep.jsonl)比较不同并发：

```bash
foretoken perf examples/quickstart \
  --dataset random --min-prompt-length 128 --max-prompt-length 256 \
  --temperature 0 --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 \
  --experiment-name concurrency --output local,wandb,plot
```

该示例在 1、2、4 并发下测量性能，固定目标输出为 256 token。每个参数点重复三次，每次重复前额外发送 16 个 HTTP 请求进行预热。

## 选择负载

常用参数文件集中在 [`scripts/common/`](../../scripts/common/)。通过 `--sweep` 传入文件，测量其中的参数组合：

| 文件 | 用途 |
| --- | --- |
| [`fixed-length.jsonl`](../../scripts/common/fixed-length.jsonl) | 比较输入输出较均衡、长输入和长输出负载下的并发 |
| [`fixed-arrival.jsonl`](../../scripts/common/fixed-arrival.jsonl) | 在固定短输入和短输出下比较请求速率 |
| [`fixed-capacity.jsonl`](../../scripts/common/fixed-capacity.jsonl) | 在固定长输入和短输出下比较并发 |
| [`long-context.jsonl`](../../scripts/common/long-context.jsonl) | 扫描输入长度；输出长度和并发由命令指定 |
| [`conversation-rate.jsonl`](../../scripts/common/conversation-rate.jsonl) | 扫描所选数据集的对话启动速率 |
| [`studychat-conversation.jsonl`](../../scripts/common/studychat-conversation.jsonl) | 扫描 StudyChat 负载的并发 |
| [`slo-thresholds.jsonl`](../../scripts/common/slo-thresholds.jsonl) | 在不同对话启动速率下比较逐请求 SLO 阈值 |
| [`quantized-models.jsonl`](../../scripts/common/quantized-models.jsonl) | 在不同并发下比较 BF16 与 4-bit 部署 |

测量固定长度负载：

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/fixed-length.jsonl --num-runs 1 --num-prompts 32 \
  --warmup-requests 4 --temperature 0 --output local,wandb,plot
```

只保留所选模型和服务能支持的长度组合与并发。模型上下文须容纳输入、输出及聊天模板的额外开销。固定输出长度要求服务支持 `min_tokens` 和 `ignore_eos`。tokenizer 从所选模型推导，可通过 `--tokenizer-path` 覆盖。

每行 JSONL 将相关设置放在一起。字段沿用 CLI 名称，将连字符换成下划线，例如 `request_rate`。列表表示扫描维度，同一行中的多个维度会展开为全部组合。每次运行混合两个数据集时，使用嵌套列表：`"dataset": [["first.jsonl", "second.jsonl"]]`。

`--slo-params` 用于统计固定负载的达标情况；添加 `--slo-search` 可[搜索并发](slo_zh.md)。与 sweep 一起使用时，`--num-runs` 控制每个参数点的完整搜索重复次数，每次搜索的探测点测量一轮。

## 比较 SLO 阈值与请求速率

[SLO 阈值配置](../../scripts/common/slo-thresholds.jsonl)扫描逐请求 TTFT 阈值和对话启动速率，固定 TPOT 阈值。将 URL 和模型名换成服务的 Chat Completions 地址和模型：

```bash
foretoken perf --url http://host/v1/chat/completions --model Qwen/Qwen3-0.6B \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/slo-thresholds.jsonl \
  --temperature 0 --random-seed 0 --max-concurrency -1 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name slo-thresholds --output local,wandb,plot
```

每个请求须同时满足两个耗时阈值。100 个请求预算按各轮 HTTP 请求计数。比较各速率下的达标率与 goodput；阈值曲线固定比较运算符和其他负载设置，速率曲线则按阈值分开展示。达标率的解读见[固定速率 SLO 测量](slo_zh.md#固定对话启动速率测量达标率)。

JSONL 中的 `"slo_params": [{"ttft": "<=250ms", "tpot": "<=100ms"}, {"ttft": "<=500ms", "tpot": "<=100ms"}]` 扫描两组条件。使用 `--slo-search` 时，可将对象放入嵌套列表，在一个选择中运行多个独立搜索：`"slo_params": [[{"p99_ttft": "<=250ms"}, {"p99_tpot": "<=100ms"}]]`。

## 测量长输入上下文

使用[长上下文配置](../../scripts/common/long-context.jsonl)，将并发设为 1、目标输出固定为 512 token：

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/long-context.jsonl \
  --min-output-length 512 --max-output-length 512 --max-concurrency 1 \
  --num-prompts 4 --warmup-requests 1 --num-runs 1 \
  --experiment-name long-context --output local,wandb,plot
```

只保留模型能处理的行：上下文须容纳输入、512 个输出 token 和聊天模板的额外开销。随机输入长度是生成目标，实际长度以服务报告的输入 token 用量为准。这组小样本用于比较输入长度带来的性能变化；评估尾延迟或 SLO 容量时，应增加请求预算并重复测量。

## 运行对话负载

比较 StudyChat 负载在不同并发下的性能：

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
  --sweep benchmarks/scripts/common/studychat-conversation.jsonl \
  --max-tokens 128 --temperature 0 --num-runs 3 \
  --warmup-requests 20 --num-prompts 1000 --output local,wandb,plot
```

使用同一个[速率配置](../../scripts/common/conversation-rate.jsonl)，分别指定 ShareGPT 和 StudyChat 数据集，比较对话启动速率：

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/conversation-rate.jsonl \
  --temperature 0 --random-seed 0 --max-concurrency -1 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name sharegpt-rate --output local,wandb,plot

foretoken perf examples/quickstart \
  --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
  --sweep benchmarks/scripts/common/conversation-rate.jsonl \
  --temperature 0 --random-seed 0 --max-concurrency -1 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name studychat-rate --output local,wandb,plot
```

速率控制对话何时启动，请求预算按各轮 HTTP 请求计数；最后一段对话可能在预算耗尽时停止。有文本参考答案的轮次按对应 token 数定长生成，没有参考答案时使用 `--max-tokens` 上限。显式长度设置见[对话输出长度](conversations_zh.md)。按记录的到达时间运行负载，见 [Mooncake 轨迹回放](mooncake-trace_zh.md)。

## 比较多种方法

传入多个 Kustomize 示例，在各服务上测量同一负载：

```bash
foretoken perf examples/quickstart examples/quickstart3 \
  --dataset random --num-prompts 100 --output local,wandb,plot
```

多个端点使用一个 `--url` 后跟多个 URL；`--model` 可以提供一个共享模型名，也可以逐个提供模型名。sweep 行中的 `service` 列表表达相同选择。[量化模型 sweep 配置](../../scripts/common/quantized-models.jsonl)用于比较 [BF16 和 4-bit 部署](../../../examples/quantized-model/README_zh.md)：

```bash
foretoken perf --dataset random --sweep benchmarks/scripts/common/quantized-models.jsonl \
  --num-prompts 100 --warmup-requests 10 --num-runs 3 \
  --temperature 0 --experiment-name methods --output local,wandb,plot
```

服务路径相对于仓库根目录解析。已有端点使用 `name`、`url`、`model`，可选 `health_url`；认证使用 `--api-key`。

一个方法的全部参数点执行完毕后，再运行下一方法。临时部署在方法切换时删除，已有服务原样复用。测量已有服务的配置变更前，先用 `foretoken deploy` 应用。

## 查看结果与重新绘图

通过 `sweep_summary.csv` 和对比图，比较相同负载设置下的延迟与吞吐量。误差线表示各轮结果的样本标准差，分位数也先逐轮计算、再汇总；只有一轮时不估计误差。比较参数点时，同时查看各指标的有效样本数和失败运行数。

重复使用同一个 `--experiment-name` 会覆盖该实验目录；省略它时创建带时间戳的新目录。将第一个示例重新绘制为双栏宽度，无需发送请求：

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

各点分别保留生成的视频与性能指标。生成参数和服务要求见[视频负载](video_zh.md)。

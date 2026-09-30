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

常用实验配置集中在 [`scripts/common/`](../../scripts/common/)。[固定长度配置](../../scripts/common/fixed-length.jsonl)用不同行保持五组输入／输出长度配对，再展开各行的并发列表。

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/fixed-length.jsonl --num-runs 3 --num-prompts 128 \
  --warmup-requests 8 --temperature 0 --output local,wandb,plot
```

五组输入／输出长度分别为 8,192/2,048、32,768/4,096、131,072/4,096、8,192/16,384 和 32,768/16,384 token。各组扫描并发 1、8、16、32，共 20 个参数点、60 次测量；每轮测量 128 个请求、预热 8 个请求，作为起始负载。选择模型和服务能支持的长度与并发；模型上下文须容纳输入及输出。并发 1 的结果同时用于单请求比较，延迟、吞吐与资源图表复用这些运行。tokenizer 从所选模型服务推导，需要覆盖时才传 `--tokenizer-path`。

负载、生成和数据集字段沿用 CLI 名称，将连字符换成下划线。例如 `request_rate: [4, 8, 16]` 扫描到达率。[固定到达率配置](../../scripts/common/fixed-arrival.jsonl)和[容量配置](../../scripts/common/fixed-capacity.jsonl)可以直接使用。列表表示扫描维度；每次运行混合两个数据集时写作 `"dataset": [["first.jsonl", "second.jsonl"]]`。加上 `--slo-search` 才搜索并发，否则只测量固定负载的达标情况；此时 `--num-runs` 控制每个点的完整搜索重复次数，每次搜索的探测点测量一轮。

## 比较 SLO 阈值与请求速率

[SLO 阈值配置](../../scripts/common/slo-thresholds.jsonl)自动下载与[到达率扫描](slo_zh.md#固定对话启动速率测量达标率)相同的 ShareGPT 对话。将 URL 和模型名换成服务的 Chat Completions 地址和模型：

```bash
foretoken perf --url http://host/v1/chat/completions --model Qwen/Qwen3-0.6B \
  --sweep benchmarks/scripts/common/slo-thresholds.jsonl \
  --num-prompts 300 --warmup-requests 0 --num-runs 3 \
  --experiment-name slo-thresholds --output local,wandb,plot
```

配置组合四档对话启动速率与 125、250、500 毫秒的首 token 耗时（TTFT）阈值，固定每输出 token 耗时（TPOT）阈值为 100 毫秒，共 12 个参数点，每点测量三轮。SLO 逐条 HTTP 请求计算达标率，300 个请求预算按完整对话的各轮共用。这是 Foretoken 的阈值敏感性负载，不是论文协议。本地和 W&B 共用逐点均值、标准差，并用曲线比较达标率与 goodput；本地图表显示标准差误差棒，选择 `plot` 时导出的图也会上传。JSONL 中的 `"slo_params": [{"ttft": "<=0.25", "tpot": "<=0.1"}, {"ttft": "<=0.5", "tpot": "<=0.1"}]` 扫描两组逐请求条件，每组均要求 TTFT 和 TPOT 同时达标。仅当一次 `--slo-search` 选择包含多个独立搜索时，才使用嵌套列表：`"slo_params": [[{"p99_ttft": "<=0.25"}, {"p99_tpot": "<=0.1"}]]`。阈值曲线固定比较运算符和其他负载设置，请求速率曲线则按阈值分开展示。

## 测量长输入上下文

使用[长上下文配置](../../scripts/common/long-context.jsonl)，在并发 1、固定目标输出 256 token 的条件下比较输入长度带来的性能变化：

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/long-context.jsonl \
  --num-prompts 16 --warmup-requests 1 --num-runs 3 \
  --experiment-name long-context --output local,wandb,plot
```

六行分别设置输入长度为 16,384、32,768、65,536、131,072、262,144 和 512,000 token。每个参数点测量 16 个请求，每轮前预热 1 个请求，共重复 3 轮。最后一档是 512,000 个输入 token，为模型实际支持的上下文中的输出和模板开销留出空间。只保留模型能处理的行：实际支持的上下文长度须容纳输入、256 个输出 token 和聊天模板的额外开销。随机输入长度是生成目标，实际长度以服务报告的输入 token 用量为准。这是 Foretoken 的长输入敏感性负载，不是统一论文协议，也不用于得出 p99／SLO 容量结论。本地和 W&B 共用各轮数据，绘制输入长度曲线并统计重复结果。

## 数据驱动负载

使用维护中的 [StudyChat 配置](../../scripts/common/studychat-conversation.jsonl)运行真实数据集：

```bash
foretoken perf examples/quickstart \
  --sweep benchmarks/scripts/common/studychat-conversation.jsonl \
  --num-runs 3 --warmup-requests 20 --num-prompts 1000 \
  --output local,wandb,plot
```

该配置扫描并发 1、8、16、32，共 4 个参数点、12 次测量。

使用相同的设置比较 [ShareGPT](../../scripts/common/sharegpt-rate.jsonl) 和 [StudyChat](../../scripts/common/studychat-rate.jsonl) 的对话启动速率：

```bash
foretoken perf examples/quickstart \
  --sweep benchmarks/scripts/common/sharegpt-rate.jsonl \
  --num-prompts 300 --warmup-requests 0 --num-runs 3 \
  --experiment-name sharegpt-rate --output local,wandb,plot

foretoken perf examples/quickstart \
  --sweep benchmarks/scripts/common/studychat-rate.jsonl \
  --num-prompts 300 --warmup-requests 0 --num-runs 3 \
  --experiment-name studychat-rate --output local,wandb,plot
```

每个配置扫描每秒启动 2、4、8、16 段对话，共 4 个参数点、每个数据集测量 12 轮。每点的 300 个 HTTP 请求预算由各段对话的轮次共享，最后一段对话可能在用完预算时停止。`max_tokens: 4096` 是每轮生成上限，不是固定输出长度。

使用维护中的 [Mooncake Conversation trace 配置](../../scripts/common/mooncake-conversation.jsonl)回放请求时序：

```bash
foretoken perf examples/quickstart \
  --sweep benchmarks/scripts/common/mooncake-conversation.jsonl \
  --num-runs 3 --output local,wandb,plot
```

数据集负载保留任务行和长度分布；trace 回放保留记录的到达时间、输出目标和共享前缀元数据。两者属于不同的负载协议。

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

结果目录包含 `sweep_summary.csv`、各次运行目录，以及 `plots/` 下的 PDF、SVG、PNG 和 CSV 文件。使用 Kustomize 服务且集群提供 Prometheus 时，猜测解码的接受率和阶段耗时估算也会在取得观测值后进入扫描汇总与对比图。统计保留每个指标的有效样本数；误差线表示各轮结果的样本标准差，分位数指标也先逐轮计算、再汇总。只有一轮时不估计误差。

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

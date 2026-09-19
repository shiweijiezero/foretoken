# 结果指标

[English](metrics.md) | 简体中文 · [常用命令](docs/examples_zh.md)

`metrics.json` 保存汇总指标，`raw_output.json` 保存逐请求记录。标准负载还保留 `benchmark_data.db` 和 `benchmark.log`。

## 实验记录

使用本地输出时，`environment.json` 保存客户端 Python 和依赖版本；源码安装还记录提交与工作区是否有修改。Kustomize 模式额外记录执行前后的模型服务设置、模型与 tokenizer 修订版本、运行配置、Pod 镜像 ID 和节点信息。快照中的 `error` 字段表示读取失败。URL 模式仅记录客户端信息。需要复现配置对比时，参阅[参数扫描](docs/coomon_commands/sweep_zh.md#对比推理配置)。

`--warmup-requests N` 在每次生成式负载运行前完成 N 段对话，包括扫描的每次重复和各数据集子运行。预热结果保存在 `warmup/`，与正式指标和 profiling 采集分开。预热复用起始数据行和随机种子；全部成功后，测量使用新的 HTTP 客户端开始运行。默认不预热，轨迹回放需要单独预热。

扫描的 `sweep_points.json` 保留每次重复，`sweep_summary.json` 和 `sweep_summary.csv` 按参数点汇总：

| 汇总字段 | 含义 |
| --- | --- |
| `runs` | 重复运行总数，包括失败的运行 |
| `samples` | 该指标的有效样本数；缺失时延不计入，零吞吐和失败数仍保留 |
| `mean`、`median`、`min`、`max` | 各次运行有效指标的均值、中位数、最小值和最大值 |
| `stddev` | 样本标准差，少于两个样本时不可用 |

以 `_seconds` 结尾的时延指标使用秒。对各次 p95 做统计反映的是运行间差异，不是将所有请求合并后计算百分位。

## 请求指标

| 指标 | 含义 |
| --- | --- |
| Success rate | 成功请求数除以尝试请求数 |
| End-to-end latency (E2EL) | 请求端到端耗时；成功的流式请求计时到最后一个 `choices` 非空分片 |
| TTFT | 从发送请求到收到首个 `choices` 非空分片的时间 |
| TPOT | `(E2EL − TTFT) / (输出 token 数 − 1)`；输出不足两个 token 时不可用 |
| ITL | 相邻 `choices` 非空分片的到达间隔；一个分片可能包含多个 token |
| Time to final-answer token (TTFAT) | 从整段对话开始到最终回答首个分片的时间 |
| Request throughput (req/s) | 成功请求数除以运行时间 |
| Input token throughput (tokens/s) | 成功请求的输入 token 总数除以运行时间 |
| Output token throughput (tokens/s) | 成功请求的输出 token 总数除以运行时间 |
| Output token throughput per user (tokens/s) | 输出吞吐量除以配置并发数；`--parallel -1` 时等于总输出吞吐量 |
| Output token throughput per GPU (tokens/s) | 输出吞吐量除以模型声明的 GPU 容量，用于扫描比较 |
| Benchmark duration (s) | 整次评测的持续时间 |

请求延迟分布只统计成功请求。`--no-stream` 保留延迟和吞吐量，不报告 TTFT、TPOT 和 ITL。仅含用量统计的分片不计入流式计时。

多轮数据的每个 HTTP 轮次是一条请求。某轮失败会终止当前对话，成功轮次数不等于成功对话数。多数据集保留各数据集的对话百分位，不直接平均。

## 曲线

每次运行结束后，W&B 提供以下视图：

- 时间曲线按一秒完成窗口展示请求数、吞吐量、失败率、耗时 p95 和平均在途请求数；最后一个窗口使用实际时长。
- 累计曲线展示已完成请求的累计数量、成功率、平均耗时和从运行开始计算的吞吐量。
- 逐请求曲线按发送顺序展示每条请求的耗时、token 数和成功状态，序号从 1 开始。
- Kustomize 评测还会按模型服务和扩缩目标记录控制器实际应用的期望副本数和 Ready 副本数。

图表和终端中的 TTFT、E2EL、会话耗时使用秒，TPOT、ITL 使用毫秒。原始 JSON 耗时仍以秒保存。

窗口 token 吞吐量把成功请求的 token 数归入其完成窗口，不表示每个 token 的实际生成时刻。没有请求完成的窗口吞吐量为零，不产生延迟或失败率样本。曲线在评测结束后上传，不是实时上传。

同一 W&B group 使用相同坐标对比。最终汇总和可用的 p50/p95/p99 也记录到 Charts，Summary 保留副本；旧运行保持原有指标名称和单位。

轨迹结果另有从计划到达到实际发送的 Replay delay。标注 `including replay delay` 的 E2EL 和 TTFT 包含这段等待。轨迹曲线按计划到达时间统计，时间曲线按实际请求完成窗口统计。

默认不重试。`--max-retries N` 允许对暂时性故障最多额外尝试 `N` 次，重试耗时计入该次逻辑请求延迟。

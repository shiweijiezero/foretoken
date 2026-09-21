# 结果指标

[English](metrics.md) | 简体中文 · [常用命令](docs/examples_zh.md)

`metrics.json` 保存汇总指标，`raw_output.json` 保存逐请求记录。标准负载还保留 `benchmark_data.db` 和 `benchmark.log`。

## 实验记录

本地结果包括：

| 文件 | 内容 |
| --- | --- |
| `environment.json` | 客户端版本与源码状态；Kustomize 模式另有执行前后的服务设置、镜像 ID 和节点信息，读取失败记录在 `error` 中 |
| `warmup/` | 预热结果，不计入正式指标和 profiling 采集 |
| `sweep_points.json` | 每次扫描运行的结果 |
| `sweep_summary.json`、`sweep_summary.csv` | 各参数点在重复运行中的均值、中位数、样本标准差及范围 |

扫描汇总中，`runs` 是重复次数，`samples` 是有效样本数。缺失时延不计入样本，零吞吐和失败数仍保留；少于两个样本时，`stddev` 不可用。以 `_seconds` 结尾的时延指标使用秒。各次 p95 的汇总不等于合并请求后的 p95。

预热复用负载的起始数据行和随机种子，全部成功后才开始测量。轨迹回放需要单独预热。

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
| `Output tok/s / user` | 输出吞吐量除以 `--parallel`；`--parallel -1` 时使用实测平均活跃请求数 |
| Output token throughput per GPU (tokens/s) | 输出吞吐量除以模型声明的 GPU 容量 |
| Mean reported cached input tokens | 成功请求中已报告的 `usage.prompt_tokens_details.cached_tokens` 平均值 |
| Benchmark duration (s) | 整次评测的持续时间 |

请求延迟分布只统计成功请求。`--no-stream` 保留延迟和吞吐量，不报告 TTFT、TPOT 和 ITL。仅含用量统计的分片不计入流式计时。

## SLO 结果

启用 `--slo-params` 后，使用延迟类条件的请求会在 `raw_output.json` 中获得 `slo_met`。汇总结果同时记录同一条件下的 SLO 达标率、请求 goodput 和 token goodput。SLO 容量搜索仍按配置的聚合条件判断探测点，并搜索满足条件的最大并发。

服务未报告 token 用量时，对应 token 数保持不可用。如果任一成功请求缺少输入或输出用量，需要完整 token 总数的汇总指标也保持不可用，不把缺失值当作零。缓存输入 token 保留服务报告的原值，包括明确报告的零；它不表示某个存储层或 KV store 的命中率。

只有负载实际执行多轮对话时才发布会话指标。每个 HTTP 轮次是一条请求。某轮失败会终止当前对话，成功轮次数不等于成功对话数。多数据集保留各数据集的对话百分位，不直接平均。

## 曲线

每次运行结束后，W&B 提供以下视图：

- 时间曲线按一秒完成窗口展示请求数、吞吐量、失败率、耗时 p95 和平均在途请求数；最后一个窗口使用实际时长。
- 累计曲线展示已完成请求的累计数量、成功率、平均耗时和从运行开始计算的吞吐量。
- 逐请求曲线按发送顺序展示每条请求的耗时、服务已报告的 token 数和成功状态，序号从 1 开始。
- Kustomize 评测还会按模型服务和扩缩目标记录控制器实际应用的期望副本数和 Ready 副本数。

图表和终端中的 TTFT、E2EL、会话耗时使用秒，TPOT、ITL 使用毫秒。原始 JSON 耗时仍以秒保存。

窗口 token 吞吐量把成功请求的 token 数归入其完成窗口，不表示每个 token 的实际生成时刻。没有请求完成的窗口吞吐量为零，不产生延迟或失败率样本。曲线在评测结束后上传，不是实时上传。

同一 W&B group 使用相同坐标对比。最终汇总和可用的 p50/p95/p99 也记录到 Charts，Summary 保留副本；旧运行保持原有指标名称和单位。

轨迹结果另有从计划到达到实际发送的 Replay delay。标注 `including replay delay` 的 E2EL 和 TTFT 包含这段等待。轨迹曲线按计划到达时间统计，时间曲线按实际请求完成窗口统计。

默认不重试。`--max-retries N` 允许对暂时性故障最多额外尝试 `N` 次，重试耗时计入该次逻辑请求延迟。

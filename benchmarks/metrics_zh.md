# 结果指标

[English](metrics.md) | 简体中文 · [常用命令](docs/examples_zh.md)

`metrics.json` 保存汇总指标，`raw_output.json` 保存逐请求记录。标准负载还保留 `benchmark_data.db` 和 `benchmark.log`。

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

轨迹结果另有 Replay delay，表示从计划到达到实际发送的等待时间。标注 `including replay delay` 的 E2EL 和 TTFT 包含这段等待。轨迹图按计划到达时间分桶，不是完成时间。

默认不重试。`--max-retries N` 允许对暂时性故障最多额外尝试 `N` 次，重试耗时计入该次逻辑请求延迟。

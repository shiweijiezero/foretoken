# 性能指标

[English](metrics.md) | 简体中文 · [性能评测示例](docs/perf/README_zh.md)

结果包括汇总指标和逐请求记录。

## 请求指标

| 指标 | 含义 |
| --- | --- |
| Success rate | 成功请求数除以尝试请求数 |
| 并发限额（`max_concurrency`） | 配置的上限：单轮和 trace 负载约束请求数，多轮负载约束对话数 |
| 实测请求并发（`request_concurrency`） | `peak` 为同时进行的请求数峰值；`mean` 为请求耗时总和除以测量时长，包含失败请求 |
| End-to-end latency (E2EL) | 请求端到端耗时；成功的流式请求计时到最后一个 `choices` 非空分片 |
| TTFT | 从发送请求到收到首个 `choices` 非空分片的时间 |
| TPOT | `(E2EL − TTFT) / (输出 token 数 − 1)`；输出不足两个 token 时不可用 |
| ITL | 相邻 `choices` 非空分片的到达间隔；一个分片可能包含多个 token |
| Time to final-answer token (TTFAT) | 从整段对话开始到最终回答首个分片的时间 |
| Request throughput (req/s) | 成功请求数除以运行时间 |
| Input token throughput (tokens/s) | 成功请求的输入 token 总数除以运行时间 |
| Output token throughput (tokens/s) | 成功请求的输出 token 总数除以运行时间 |
| `Output tok/s / user` | 输出吞吐量除以 `--max-concurrency`；`--max-concurrency -1` 时使用实测平均活跃请求数 |
| Output token throughput per GPU (tokens/s) | 输出吞吐量除以模型声明的 GPU 容量 |
| Mean reported cached input tokens | 成功请求中已报告的 `usage.prompt_tokens_details.cached_tokens` 平均值 |
| Benchmark duration (s) | 整次评测的持续时间 |

请求延迟分布只统计成功请求。`--no-stream` 保留延迟和吞吐量，不报告 TTFT、TPOT 和 ITL。仅含用量统计的分片不计入流式计时。

## SLO 结果

启用 `--slo-params` 后，使用延迟类条件的请求会在 `raw_output.json` 和 W&B 逐请求曲线中获得 `slo_met`。CLI、`metrics.json` 和 W&B Summary 同时记录同一条件下的 SLO 达标率、请求 goodput 和 token goodput。[SLO 并发搜索](docs/perf/slo_zh.md) 按聚合条件判断探测点，报告实测最高达标请求峰值、对应配置限额和停止原因。

服务未报告 token 用量时，对应 token 数保持不可用。如果任一成功请求缺少输入或输出用量，需要完整 token 总数的汇总指标也保持不可用，不把缺失值当作零。缓存输入 token 保留服务报告的原值，包括明确报告的零；它不表示某个存储层或 KV store 的命中率。

只有负载实际执行多轮对话时才发布会话指标。每个 HTTP 轮次是一条请求。某轮失败会终止当前对话，成功轮次数不等于成功对话数。多数据集保留各数据集的对话百分位，不直接平均。

图表中的 TTFT、E2EL 和会话耗时使用秒，TPOT 和 ITL 使用毫秒。原始 JSON 耗时仍以秒保存。轨迹结果还会记录计划到达与实际发送之间的 replay delay。

默认不重试。`--max-retries N` 允许对暂时性故障最多额外尝试 `N` 次，重试耗时计入该次逻辑请求延迟。

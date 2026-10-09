# 性能指标

[English](metrics.md) | 简体中文 · [性能评测示例](perf/README_zh.md)

结果包括汇总指标和逐请求记录。

## 请求指标

| 指标 | 含义 |
| --- | --- |
| Success rate | 成功请求数除以尝试请求数 |
| 并发限额（`max_concurrency`） | 配置的上限：单轮和 trace 负载约束请求数，多轮负载约束对话数 |
| 实测请求并发（`request_concurrency`） | `peak` 为同时进行的请求数峰值；`mean` 为请求耗时总和除以测量时长，包含失败请求 |
| End-to-end latency (E2EL) | 请求端到端耗时；成功的流式请求计时到最后一个 `choices` 非空分片 |
| TTFT | 从发送请求到收到首个 `choices` 非空分片的时间 |
| TPOT | `(E2EL − TTFT) / (输出 token 数 − 1)`；单 token 输出为零；缺少输出用量或流式计时时不可用 |
| ITL | 相邻 `choices` 非空分片的到达间隔；一个分片可能包含多个 token |
| Request throughput (req/s) | 成功请求数除以运行时间 |
| Input token throughput (tokens/s) | 成功请求的输入 token 总数除以运行时间 |
| Output token throughput (tokens/s) | 成功请求的输出 token 总数除以运行时间 |
| `Output tok/s / user` | 输出吞吐量除以 `--max-concurrency`；`--max-concurrency -1` 时使用实测平均活跃请求数 |
| Output token throughput per GPU (tokens/s) | 输出吞吐量除以模型声明的 GPU 容量 |
| Mean reported cached input tokens | 成功请求中已报告的 `usage.prompt_tokens_details.cached_tokens` 平均值 |
| Benchmark duration (s) | 整次评测的持续时间 |

请求延迟分布只统计成功请求。`--no-stream` 保留延迟和吞吐量，不报告 TTFT、TPOT 和 ITL。仅含用量统计的分片不计入流式计时。

## 曲线

W&B 分别展示每次运行所测模型的输入和输出吞吐量。`Time` 曲线按一秒窗口统计：成功请求结束时，将它的全部 token 计入该窗口，再除以窗口时长。`Cumulative` 曲线将已经完成的成功请求 token 总数除以经过时间。两者只统计本轮评测流量；Grafana 的模型总计则覆盖到达模型的全部流量。

时间曲线还展示请求吞吐量、在途请求数、失败率和延迟分位数；逐请求曲线按发送顺序排列。同一 group 可对比不同运行，参数扫描的帕累托图用于比较吞吐量与延迟的取舍。

## GPU 分配量

Kustomize 性能评测会在与负载、Ready 副本和 SLO 窗口相同的经过时间轴上采样已分配 GPU 数量。`gpu_allocation.json` 保留边界样本、未知时间段和读取失败时刻。`metrics.json` 按设备资源名分别记录 `gpu_seconds` 与 `gpu_hours`，并记录观测覆盖率。覆盖不完整时，完整窗口总量保持不可用；`observed_gpu_seconds` 只是部分观测面积，不是总量。不同 NVIDIA、MetaX 或其他设备类型不会平均或合并。明确观测到的零分配是有效数据。

## 猜测解码观测

使用 Kustomize 模型服务且集群提供 Prometheus 时，性能评测会报告已接受草稿 token 占提出草稿 token 的比例、每次草稿迭代接受的 token 数、每个计时步骤的草稿与目标模型 forward 平均 GPU 耗时，以及草稿和目标模型 forward 各自在两段合计 GPU 耗时中的占比。目标模型 forward 包含 batch 验证计算，不含采样和拒绝处理；阶段耗时不是请求延迟，也不能直接当作加速比。模型服务计数器统计到达该服务的全部流量，包括本次评测之外的请求。

`metrics.json` 与 W&B Summary 通过本次测量窗口内的 Prometheus 计数器增量估算这些值，并处理计数器重置。运行太短而缺少足够抓取样本，或服务没有猜测解码指标时，数值保持不可用，不填零。Prometheus 时间曲线则展示各采样时刻过去五分钟的速率，不代表整次测量的汇总。参数扫描保留逐轮估算值，在取得样本时汇总均值与标准差。

## SLO 结果

启用 `--slo-params` 后，使用延迟类条件的请求会在 `raw_output.json` 和 W&B 逐请求曲线中获得 `slo_met`。CLI、`metrics.json` 和 W&B Summary 同时记录同一条件下的 SLO 达标率、请求 goodput 和 token goodput。失败或缺少必要计时指标的请求计为不达标。达标率是满足全部条件的请求占实测请求的比例；请求 goodput 和 token goodput 分别是达标请求数及其输出 token 总数除以运行时长。设置逐请求条件后，一秒完成窗口的时间曲线也展示达标率、请求 goodput 和 token goodput。失败请求计入窗口达标率分母；没有完成请求的窗口不显示达标率。整次运行的汇总仍按全部实测请求计算，不平均各窗口达标率。加上 `--slo-search` 后启用 [SLO 并发搜索](perf/slo_zh.md)，按聚合条件判断探测点，报告实测最高达标请求峰值、对应配置限额和停止原因。

服务未报告 token 用量时，对应 token 数保持不可用。如果任一成功请求缺少输入或输出用量，需要完整 token 总数的汇总指标也保持不可用，不把缺失值当作零。缓存输入 token 保留服务报告的原值，包括明确报告的零；它不表示某个存储层或 KV store 的命中率。

对话负载报告已开始和已完成的对话数、HTTP 轮次数、每段对话的平均轮次，以及对话启动吞吐量。某轮失败会终止当前对话，成功轮次数不等于成功对话数。

图表中的 TTFT 和 E2EL使用秒，TPOT 和 ITL 使用毫秒。原始 JSON 耗时仍以秒保存。轨迹结果还会记录计划到达与实际发送之间的 replay delay。

默认不重试。`--max-retries N` 允许对暂时性故障最多额外尝试 `N` 次，重试耗时计入该次逻辑请求延迟。

# 性能指标

[English](metrics.md) | 简体中文 · [性能评测示例](perf/README_zh.md)

结果包括汇总指标和逐请求记录。

## 请求指标

| 指标 | 含义 |
| --- | --- |
| 已记录请求（`request_num`） | 已记录成功或失败结果的请求数；运行中断时，尚未结束的请求可能没有记录 |
| 已准备请求（`planned_num`） | 测量前准备好的 HTTP 请求总数，按对话轮次计数，可能与 `num_prompts` 不同；仅按时长运行、未限定请求总数时不提供 |
| 已接纳请求（`admitted_num`） | 调度器已接纳的独立请求数，或已接纳对话中的总轮次数 |
| 未执行请求（`not_executed_num`） | 已准备但没有结果记录的请求数；未限定请求总数时，只统计已接纳对话中的剩余轮次 |
| Success rate | `success_num / request_num`；跳过的轮次不算 HTTP 请求失败 |
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

多轮对话中，某轮失败后会跳过剩余轮次，不再补发。`not_executed_reasons` 区分前序请求失败（`failed_predecessor`）和达到时长限制（`duration`）；`failure_reasons` 按 HTTP 状态和错误信息汇总已记录的失败请求。

对话负载还报告已开始和已完成的对话数、HTTP 请求轮次数、每段对话的平均轮次，以及每秒启动的对话数。成功请求数与成功完成的对话数分别统计。

请求延迟分布只统计成功请求。`--no-stream` 保留延迟和吞吐量，不报告 TTFT、TPOT 和 ITL。仅含用量统计的分片不计入流式计时。

ITL 分位数的相对数值误差为 1%。逐请求 ITL SLO 使用最大间隔判定。

## 曲线

W&B 分别展示每次运行所测模型的输入和输出吞吐量。`Time` 曲线按一秒窗口统计：成功请求结束时，将它的全部 token 计入该窗口，再除以窗口时长。`Cumulative` 曲线将已经完成的成功请求 token 总数除以经过时间。两者只统计本轮评测流量；Grafana 的模型总计则覆盖到达模型的全部流量。

时间曲线还展示请求吞吐量、在途请求数、失败率和延迟分位数；逐请求曲线按发送顺序排列。同一 group 可对比不同运行，参数扫描的权衡图用于比较吞吐量与延迟的取舍。

## 绘图派生指标

`foretoken plot` 从已保存结果计算以下指标，不改写原始测量：

| 指标 | 含义 |
| --- | --- |
| `decode_interactivity`，统计量为 `p<N>` | 基于 TPOT 的解码速度，单位 tokens/s/user：`1 / P<N>(TPOT)`，先对成功请求的 TPOT 按最近秩法取分位数，再求倒数。`N` 可为 0 到 100 之间的整数或小数，例如 `50`、`95`、`99.9`。单 token 响应没有解码间隔，不参与计算；需要完整的成功请求输出数量，以及符合条件请求的 TPOT |
| `total_tokens_per_second_per_gpu` | 输入加输出 token 的每秒总量除以已记录的 GPU 数。输入用量已经包含缓存部分 |
| `cached_prompt_tokens_per_second_per_gpu` | 每 GPU 每秒缓存输入 token 数，需要所有成功请求均报告缓存输入用量 |
| `uncached_prompt_tokens_per_second_per_gpu` | 每 GPU 每秒非缓存输入 token 数，按输入减缓存输入计算，需要完整的输入与缓存输入用量 |

解码速度先取指定的 TPOT 分位数，再求倒数，而不是对逐请求速度取分位数。它使用逐请求分布，与 `itl` 汇总的所有分片间隔分布不同，不含 TTFT，也不同于输出吞吐量除以配置并发数。派生值先按每次运行计算，再汇总重复运行的均值与标准差。

CLI 分别选择指标与统计量，导出记录用 `decode_interactivity_p95` 这类字段名标识实际数值，权衡图和收益 CSV 也保留所选指标与统计量。延迟、TTFT、TPOT 和 ITL 的摘要使用保存结果中已有的统计量；逐请求 ITL 摘要不能用于重建所有分片间隔的总体分位数。

权衡图的两轴使用两个指标均可用的重复运行；有速度下限的收益选点也使用收入与速度同时可用的重复运行。对应 CSV 同时保留 `paired_samples`、完整尝试次数及失败次数；标量对照表则分别统计每个指标的可用重复运行。

每 GPU 指标使用 `metrics.json` 记录的本次测量 `gpu_count`；不含该字段的旧结果，在模型匹配时可使用 `environment.json` 的 `before.declared_gpu_count`。`--gpu-count` 用于补充本次测量缺失的 GPU 总数，若与已记录的本次测量容量冲突则报错。收益图按显式提供的非缓存输入、缓存输入和输出价格，对相应用量计价。每 GPU 每小时收入为 `3600 × sum(tokens/s/GPU × 每百万 token 价格) / 1,000,000`，利润再减去配置的 GPU 每小时成本。缺失的 token 用量保持不可用，包括缓存用量。

## GPU 分配量

Kustomize 性能评测会在与负载、Ready 副本和 SLO 窗口相同的经过时间轴上采样已分配 GPU 数量。`gpu_allocation.json` 保留边界样本、未知时间段和读取失败时刻。`metrics.json` 按设备资源名分别记录 `gpu_seconds` 与 `gpu_hours`，并记录观测覆盖率。覆盖不完整时，完整窗口总量保持不可用；`observed_gpu_seconds` 只是部分观测面积，不是总量。不同 NVIDIA、MetaX 或其他设备类型不会平均或合并。明确观测到的零分配是有效数据。

集群提供加速器指标时，`prometheus_observations.json` 还记录采样到的物理设备身份，包括厂商、设备 ID、节点和 Pod。`environment.json` 保留运行期间 Pod、容器、镜像与运行配置的变化。

## CPU 与内存观测

Kubernetes 服务具备 Prometheus 时，`metrics.json.resources` 和 W&B 展示测量窗口内的平均 CPU 使用核数、发生限流的 CPU 调度周期比例及累计限流秒数。这些值由容器计数器增量估算，包含运行中被替换的 Pod；抓取数据缺失或不足时保持不可用。

资源曲线展示过去一分钟的 CPU 使用率和限流周期比例，以及内核 CPU 配额和容器工作集内存。CPU 配额以核数展示；原始每周期配额的单位是微秒；内存单位为字节。查询和采样结果保存在 `prometheus_observations.json`。

## 猜测解码观测

使用 Kustomize 模型服务且集群提供 Prometheus 时，性能评测会报告已接受草稿 token 占提出草稿 token 的比例、每次草稿迭代接受的 token 数、每个计时步骤的草稿与目标模型 forward 平均 GPU 耗时，以及草稿和目标模型 forward 各自在两段合计 GPU 耗时中的占比。目标模型 forward 包含 batch 验证计算，不含采样和拒绝处理；阶段耗时不是请求延迟，也不能直接当作加速比。模型服务计数器统计到达该服务的全部流量，包括本次评测之外的请求。

`metrics.json` 与 W&B Summary 通过本次测量窗口内的 Prometheus 计数器增量估算这些值，并处理计数器重置。运行太短而缺少足够抓取样本，或服务没有猜测解码指标时，数值保持不可用，不填零。Prometheus 时间曲线则展示各采样时刻过去五分钟的速率，不代表整次测量的汇总。参数扫描保留逐轮估算值，在取得样本时汇总均值与标准差。

## SLO 结果

启用 `--slo-params` 后，使用延迟类条件的请求会在 `raw_output.json` 和 W&B 逐请求曲线中获得 `slo_met`。CLI、`metrics.json` 和 W&B Summary 同时记录同一条件下的 SLO 达标率、请求 goodput 和 token goodput。失败或缺少必要计时指标的请求计为不达标。达标率是满足全部条件的请求占实测请求的比例；请求 goodput 和 token goodput 分别是达标请求数及其输出 token 总数除以运行时长。设置逐请求条件后，一秒完成窗口的时间曲线也展示达标率、请求 goodput 和 token goodput。失败请求计入窗口达标率分母；没有完成请求的窗口不显示达标率。整次运行的汇总仍按全部实测请求计算，不平均各窗口达标率。加上 `--slo-search` 后启用 [SLO 并发搜索](perf/slo_zh.md)，按聚合条件判断探测点，报告实测最高达标请求峰值、对应配置限额和停止原因。

服务未报告 token 用量时，对应 token 数保持不可用。如果任一成功请求缺少输入或输出用量，需要完整 token 总数的汇总指标也保持不可用，不把缺失值当作零。缓存输入 token 保留服务报告的原值，包括明确报告的零；它不表示某个存储层或 KV store 的命中率。

图表中的 TTFT 和 E2EL使用秒，TPOT 和 ITL 使用毫秒。原始 JSON 耗时仍以秒保存。轨迹结果还会记录计划到达与实际发送之间的 replay delay。

默认不重试。`--max-retries N` 允许对暂时性故障最多额外尝试 `N` 次，重试耗时计入该次逻辑请求延迟。

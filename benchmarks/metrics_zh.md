# 结果指标

[English](metrics.md) | 简体中文 · [常用命令](docs/examples_zh.md)

`metrics.json` 保存汇总指标，`raw_output.json` 保存逐请求记录。标准负载还保留 `benchmark_data.db` 和 `benchmark.log`。

## 实验记录

使用本地输出时，`environment.json` 保存客户端 Python 和依赖版本；源码安装还记录提交与工作区是否有修改。Kustomize 运行额外记录执行前后的 ModelService 配置、所属 ModelGroup 的模型/tokenizer 修订版本和运行参数、Pod 镜像 ID、所在节点及节点软件信息。这些是部署状态快照，不能证明每个请求由哪个实例处理。读取失败会明确标记快照不完整。

URL 无法提供可信的硬件或部署信息，因此只记录客户端信息，不生成服务器环境快照。请随结果保存服务器实际 GPU 型号/数量、驱动和推理引擎版本、权重/tokenizer 修订版本及引擎设置。即便使用 Kustomize，`main` 这样的修订名仍可能变化，需要另存解析后的提交或本地文件版本。客户端源码有修改时也要保留改动。环境快照不会重置缓存、阻止服务更新，也无法识别运行期间的每次短暂变化。

`--warmup-requests N` 在每次生成式负载运行前完成 N 段对话，包括扫描中的每次重复和各数据集子运行。预热请求必须全部成功，本地结果保存在 `warmup/`，不计入正式指标，也不会进入 profiling 采集窗口。默认值为零。预热复用相同的起始数据行和随机种子，可能填充前缀缓存，不保证数据与测量互斥，也不保证已达到稳态。若实验要求数据互斥，请通过独立预热命令选择不同数据行。轨迹回放也需要单独预热。

预热和测量之间，模型服务保持运行。预热会等待所有请求结束，测量使用新的 HTTP 客户端，因此正式测量开始时没有尚未完成的预热请求，且可能包含建立连接的开销。这种方式预热了服务端，但不会在两个阶段之间保持连续负载。

扫描的 `sweep_points.json` 保留每次重复；`sweep_summary.json` 和 `sweep_summary.csv` 按参数点汇总跨运行的均值、中位数、样本标准差、最小值和最大值。`runs` 是重复次数，`samples` 是该指标的有效样本数。缺失时延不计入该时延指标的样本，失败数和零吞吐仍计入统计。少于两个样本时标准差不可用。各次 p95 的均值或中位数**不等于**合并请求后的 p95。以 `_seconds` 结尾的时延汇总列使用秒。

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

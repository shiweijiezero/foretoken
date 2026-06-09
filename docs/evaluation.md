# 评测指南

如何用 Foretoken 跑一次可信的 goodput 评测:为什么测 goodput、怎么跑、如何把负载匹配到硬件、以及保证结论可信的严谨性清单。指标的精确定义见[指标定义](metrics.md)。

## 为什么测 goodput

裸吞吐(输出 tok/s)在过载时依然很高,但其中大量 token 违反延迟 SLO、对用户不可交付。Foretoken 以 **goodput**(满足 SLO 的有效吞吐)为首要指标:原始吞吐随负载升到容量天花板即不再增长,而 goodput 见顶后会随负载下跌(拥塞崩溃)。盲目堆并发不增「可交付」吞吐,只增尾延迟与浪费;运行点应锁在 goodput 拐点附近。

可信评测的前提是负载真实:**真实时间戳**(突发性)+ **真实长度分布** + **真实前缀复用结构**,三者缺一,KV 相关评测即失真。

## 评测负载

默认使用公开数据集 `weijiezz/foretoken-trace`(三 split):把 Mooncake 生产 trace 的真实时序 / 并发 / 会话结构,与真实多轮对话内容缝合,回放时真模型现场生成回复。构建方法与各 split 的结构画像见[负载画像](workload-profiles.md)。也可用 `--dataset` 接本地 `.jsonl` / `.parquet` / 目录。

## 怎么跑

三种后端形式见 [README](../README.md);三者回放逻辑一致,区别在请求送到哪、测到哪一层。

```bash
CUDA_VISIBLE_DEVICES=0 bash scripts/bench.sh \
  --model <weights|HF id> --config config/models/<model>.toml \
  --split conversation --window 0:10 --rate 20
# 全部参数:python -m foretoken.bench.replay --help
```

常用参数:

- `--config`:模型配置(`[sampling]` 官方采样 + `[serve]` 引擎并行),见[模型参数](model-params.md);缺省 `config/models/default.toml`(贪心)。
- `--window N` 或 `A:B`(分钟):截取数据集的时间片。
- `--rate R`(req/min,会话级下采样,自动换算 `total_requests = R × 窗口分钟数`)或 `--total-requests N`:把负载匹配到硬件(见下);`--sec-multiplier` 拉伸 / 压缩时间轴。
- `--slo TTFT_ms:TPOT_ms`(可重复):定义 goodput 达标阈值阶梯。
- `--deadline SEC` / `--tail-grace MIN`(默认 5;墙钟上限 = 窗口跨度 × sec_multiplier + 宽限):到点取消运行中请求,截断长尾(个别高温采样会一路顶到 max_tokens 拖垮整轮)。
- `--tag`:标注优化变体(用于对比);`--param` / `--engine-param`:透传任意采样 / 引擎字段做 A/B;`--seed`:固定可复现;`--cases off|sample|full`:是否保存逐轮输入输出。

## 负载匹配硬件

真实 trace 是集群级到达,单实例 1× 全量回放必然过载(TTFT 飙到分钟级)。`--rate` / `--total-requests` 做会话级下采样(整会话保留 = 负载均衡分给本实例的份额),把负载匹配到硬件。扫不同 `--rate` 得到 **goodput-vs-load 曲线**,拐点即该配置的可持续容量:

```bash
python -m foretoken.bench.report --sweep --x rate results/runs/<A> results/runs/<B> ...
```

## 产出与对比

每个 run 落 `results/runs/<…>/`(`run.json` / `turns.jsonl` / `engine_stats.jsonl` / `summary.md` / 双语图);`turns.jsonl` 保留每轮原始指标,可事后换 SLO 重算或重画图。跨 run 排行榜在 `results/runs/INDEX.md`。

多组对比:

```bash
bash scripts/compare.sh results/runs/<A> results/runs/<B> ...
```

出 `results/compare/<时间戳>/summary.md`(每次独立留存):全指标对比表 + 对比图(CDF 叠加 / 分位柱 / goodput / 原始-vs-good 吞吐,双语)。

## 评测严谨性

做受控对照:只改被测的那个策略,其余全部固定一致。否则结论不可归因。

- **受限容量**:把缓存容量设到工作集 > 容量,否则不发生驱逐,缓存策略的差异测不出来。
- **各 arm 一致**:trace / 种子 / 容量 / 模型 / 并行 / CUDA-graph / warmup / SLO 完全相同,只有被测策略不同;≥3 个种子 + 报 95% 置信区间。
- **报 goodput 而非裸吞吐**;只计完整完成的请求;TTFT 与 TPOT 分开报。
- **prefill / decode 不混杂统计**;CUDA-graph 与 eager 不混用(避免 eager 基线对比 graph 处理组)。
- **无损校验**作为硬约束,随每个 goodput 数字一并声明(开优化的输出 == 原生输出);做法见[测试与正确性](testing.md)。

## 对标基线

- **缓存 / KV 策略**:原生 LRU 前缀缓存(主要对比对象)+ 离线最优(Belady oracle,容量上界);把价值感知策略报告为「弥合 LRU→最优差距的比例」。
- **投机解码**:no-spec 基线;报接受率与加速比(见[投机解码 MTP](mtp.md))。

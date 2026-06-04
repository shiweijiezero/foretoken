# 04 — 负载与评测方法论

评测从一开始就需可信。核心命题不是"缓存是否有用"(LRU APC 已证明这一点),而是"一个
**价值/复用感知**的策略,能否在**内存压力下的长生命周期、时间上分隔的复用**上击败 **LRU**"。
这决定了方法论:负载需要一个**时间维度** + **重尾的热块流行度**,目标是**联合 SLO 下的
goodput**(非吞吐,非瞬时命中率),并以 **Belady 最优**为界。

## Trace(优先级顺序）

| trace | 复用结构 | 时间间隔 | 内容 | 用途 |
|---|---|---|---|---|
| **Mooncake**(kvcache-ai/Mooncake,FAST'25) | 有 —— `hash_ids`(块=512 tok) | 有(1h、ms) | 无 | **主**复用 + 到达驱动 |
| vLLM×Mooncake agentic(Codex/SWE) | 有 | 有(轮间空闲 中位 5.2s / P99 81.4s) | 无 | 时间维度模型 |
| Azure 2023/2024(AzurePublicDataset) | 无 | 有(到达) | 无 | 仅到达 + 长度重尾 |
| BurstGPT | 无 | 有(突发) | 无 | 高并发 / 突发 |
| ShareGPT / LMSYS-Chat-1M | 轮次结构 | 合成 | 有 | 逐字节相同的多轮前缀 |

Mooncake schema:`{timestamp_ms, input_length, output_length, hash_ids[]}`;共享的前导
hash_ids = 共享前缀(块 512 tok)。真实的 Kimi 上界:即便在 ∞ 容量下也只有 ~50% 的 KV 可
复用;命中率随容量从 1k→50k 块由 30%→50%。**目标区间为 30–50%,而非退化微基准的 90%+。**
>50% 的块从不被复用;少数命中数万次(重尾)。

## 基准构造

- **尊重真实时间戳**(不压缩时间):保留让驱逐决策变得重要的到达间隔。vLLM
  `bench serve --dataset-name timed_trace --timed-trace-chunk-hash-size 512`。
- **内存压力是必需条件**:把 GPU KV(及各层)的容量设到**工作集 > 容量**,否则不会发生驱逐,
  策略也就未被测到。
- **时间维度**(关键 —— 缺失时 LRU ≈ 价值感知):把 Mooncake 在数小时跨度上循环 K 次,并携带
  一个延续的热 hash_id 集合(携带比例取自离线复用距离的尾部);对于多轮,注入轮间空闲
  (中位 5.2s / P99 81.4s),使缓存必须**挺过 LRU 在负载下本会驱逐的那个点**。
- 合成生成器(`generated-shared-prefix`、`prefix_repetition`)仅作健全性微基准(无时间维度、
  无重尾),不作为头条结果。

## 指标

- **主指标:goodput** = 在**联合 TTFT_P90 ≤ a× + TPOT_P90 ≤ b×** 基线(Mooncake 用 10× / 5×)、
  ≥90% 达标率下,满足 SLO 的最大 req/s;**仅完整完成的请求计数**。
- **每 GPU 字节 goodput**:缓存管理效率指标(用更少内存达到同等 goodput,或在等内存下达到更
  高 goodput);将策略与硬件隔离。
- **长生命周期复用的价值**(瞬时命中率 ≠ 价值):**命中率-vs-容量曲线**;**按复用距离分层的命中率**
  (增益必须集中在 ≥min 的长距离桶 —— 这是 LRU 最先丢掉的部分);**驱逐后即被需要的计数**
  (KVFlow 的失败模式);**节省的重算 token 数**;**弥合的 LRU→Belady 差距比例**(离线 oracle)。
- **延迟**:分别报告 TTFT/TPOT 的 **P50/P99 分布**(缓存影响 TTFT;TPOT 的变动可疑,需调查
  混杂因素)。token 级命中率(而非请求级)。
- ≥3 个种子 + 95% 置信区间;各组(arm)的 trace/种子/容量/模型/并行/CUDA-graph/预热/SLO
  完全一致,仅策略不同。

## 基线(等量总 cache-byte 预算,其余全相同)

无复用(APC 关闭)→ **原生 vLLM APC(LRU）**（主要对比对象)→ vLLM APC +
CPUOffloadingManager → LMCache → SGLang HiCache(跨系统参考,非受控消融)。禁用/固定正交的
加速器(投机解码),使增量可归因于缓存管理。

## 陷阱与修复

1. **随机 token 退化**(随机 token 从不共享前缀 → 假/零命中;并扭曲 MoE 路由)→ 用**真实
   trace 的 hash_ids** 或**真实内容**(ShareGPT)驱动复用。
2. **prefill vs decode 混杂**(缓存只影响 TTFT)→ 分别报告 TTFT/TPOT。
3. **批/并发效应** → 固定到达过程;把并发作为显式轴来扫;报告 goodput 而非吞吐。
4. **冷热偏差** → 丢弃固定的预热,测量稳态,经 `/reset_prefix_cache` 保证相同的起始状态,并
   报告该条件。
5. **CUDA-graph vs eager** → 所有组用相同的 graph 设置(避免 eager 基线 vs graph 处理组)。
6. **目标是 goodput 时却报吞吐** → SLO 下的 goodput 为头条。
7. **goodput 失真**(晚放弃 / 拖延 token 以操纵 TBT)→ 用承诺相对的截止时间,把重新发起/
   放弃的计入负载,禁止拖延 token(arXiv:2410.14257)。
8. **单区域 ≠ 多节点** → 对多节点,固定实例数 + 报告路由策略。
9. **无限容量上界的混淆** → 始终说明容量;给出命中-vs-容量曲线;在每个容量下与 Belady 比较。
10. **回放失真** → 见下面的门槛零。

## 两个构建期验证门槛(在任何策略实验之前执行)

- **门槛零(回放正确性)**:把 Mooncake 经**原生 vLLM APC** 回放,确认**实测的 token 级命中
  率 ≈ 离线从 hash_ids 算出的命中率**(差几个百分点以内)。若二者背离,则 hash_id→逐字节相
  同 token 的重建有误,所有数字无效。(在代码里核实驱动的 hash→token 确定性,不要假设;已对
  AIPerf 和 vLLM Mooncake-store connector 路径标注。)
- **Belady oracle**:从 trace 计算先知最优命中率(所有未来复用都离线已知);把价值感知 vs
  LRU 报告为**弥合的 LRU→Belady 差距比例**。

## 可证伪的 MVP 门槛(运行前先承诺)

一个价值感知的 OffloadingManager,在**等量总 cache-byte 预算**下,在**长生命周期 Mooncake
回放**上(实时时间戳、工作集 > GPU 容量、30–50% 的结构性命中上界),以 **≥15% goodput**
击败**原生 vLLM APC(LRU）**（在联合 TTFT_P90≤10× & TPOT_P90≤5× 基线、≥90% 达标率下满足
SLO 的 req/s),增益**集中在长复用距离桶(≥1 分钟)**,在 **≥3 个种子上可复现且 95% 置信区
间互不重叠**,**弥合 ≥40% 的 LRU→Belady 命中率差距**,且**无 TPOT 回退**。

次要:在 agentic 多轮 trace 上 ≥10% goodput;在匹配 goodput 下的每 GPU 字节 goodput 内存效率
论断;在合成微基准上没有赢是一个危险信号(意味着仅在 LRU 已经赢的地方有帮助)。该门槛无法
靠退化、冷热偏差、prefill/decode 混杂、无限容量上界或 goodput 失真通过。

## 来源

Mooncake 2407.00079 + 仓库 traces;vLLM×Mooncake agentic 博客(2026-05-06);Azure(Splitwise
2311.18677 / DynamoLLM 2408.00741);BurstGPT 2401.17644;LMSYS-Chat-1M 2309.11998;AttentionStore
2403.19708;KVFlow 2507.07400;DistServe 2401.09670;Revisiting-SLO/Goodput 2410.14257;SGLang
HiCache(LMSYS 2025-09-10);LMCache 2510.09665;vLLM APC + bench serve 文档;NVIDIA AIPerf / SPEED-Bench。

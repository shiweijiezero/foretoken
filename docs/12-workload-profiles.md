# 12 —— 真实业务负载画像(reasoning / agentic / 多轮 / RAG / 长上下文)

`docs/04` 是评测**方法论**、`docs/07` 是评测**实操**、`docs/06` 是 `P(reuse)` 估计器。本篇补上三者
共同依赖却尚未系统化的那块——**负载到底长什么样**。**负载画像不是背景介绍,它直接决定两件事:
(1) KV 复用价值的上界(没复用结构就没 value-aware 可言);(2) 评测真实性(长度/复用/到达三要素
失真,goodput 数全废,见 `docs/04` 门槛零)。** 现有 docs 覆盖了 4 类负载骨架(agentic/多轮/RAG/
长上下文),但缺 **reasoning 实测结构、跨节点到达、租户异质 SLO、与 llm-d/Dynamo/AIBrix 的对标维度**。
本篇补齐,并据此给出对 `docs/07` 缝合法的具体修订。

> **一句话立意**:**reasoning 是"长 output、几乎零 KV 复用"的新物种;agentic 是"巨前缀 + 高复用、
> 但被动态工具结果污染"的复用金矿;RAG 是"前缀复用结构性破裂"的硬骨头。** 三者把 KV/MTP 优化拉向
> 不同极端 —— **评测若只跑多轮对话,会系统性高估 KV 复用价值、低估 reasoning 对 MTP/decode 的依赖。**

---

## 1. 为什么负载画像决定 KV 复用价值与评测真实性

**KV 复用价值 = 复用结构(谁能命中)× 复用时距(命中前会不会被驱逐)× 内存压力(容不容得下)。**
三者全是负载属性,不是引擎属性:

- **复用结构**决定**理论命中天花板**。生产实测:Mooncake 即便 ∞ 容量也只 ~50% KV 可复用,>50% 的块
  从不被复用(`docs/04`);"KVCache in the Wild" 实测 **10% 的块贡献 77% 的复用**,trace A(to-C)
  整体命中 **62%**、trace B(to-B)**54%**([§3, 2506.02634](https://arxiv.org/html/2506.02634v5))。
  → **退化微基准里 90%+ 命中是假的;真实区间 30–60%。**
- **复用时距**决定 **value-aware 有没有用武之地**。实测复用是**短视野**的:to-C 80% 复用 <10 分钟、
  to-B <10 秒、to-B 的 KV P99 寿命仅 97 秒([§1, 2506.02634](https://arxiv.org/html/2506.02634v5))。
  → **长视野复用是又薄又高价值的尾巴(`docs/06` B8 的全部立意)。**
- **不同负载把这三个轴拉到完全不同的位置**(见 §2):reasoning 几乎无跨请求复用(CoT 复用率 **2.2%**)、
  agentic 复用率 **>92%**、RAG 前缀复用**结构性破裂**。**用错负载 = 测错优化。**

**评测真实性同理**:ServeGen(NSDI'26)实测,把"到达 trace + 长度数据集"朴素拼接,会导致
**~50% 的容量误判**(P99 TTFT 2.25s / TBT 0.5s 下,朴素法估 12 实例、实际需 25;ServeGen 误差 4%)
([§6.3 Fig.20, 2505.09999](https://arxiv.org/pdf/2505.09999))。**这就是 `docs/07` 缝合法存在的根本理由。**

---

## 2. 五类负载画像表(★ 核心)

> 单位:token。标 **[实测]** = 有一手 trace/论文实测;标 **[估]** = 区间估计/单源,需进一步核实;
> 标 **[待核实]** = 拿不准,只给依据。**长度分布尤其不敢编 —— 不确定的全部降级标注。**

| 负载 | input 长度 | output 长度 | 复用结构(命中来源) | 复用率 / 时距 | 到达模式 | 数据来源(一手) |
|---|---|---|---|---|---|---|
| **reasoning**(R1/o1/GLM-think) | **均值 ~800** [实测,MLPerf DS-R1];SLOs-Serve 推理任务 prompt ~127 [实测] | **均值 ~3,880(含 thinking)、max 20K** [实测,MLPerf];reason ≈ **4× answer** [实测,ServeGen];比非推理模型 **~8×** 长 [实测,Epoch] | **几乎无跨请求复用**:CoT/thinking **2.2% 复用**(intra-conv 4.2% / inter-conv **0.0%**)[实测,SAECache] | **极低**;CoT 由随机解码生成、query-specific,**驱逐优先级最低** [实测,SAECache w=0.02] | **不那么突发**:CV 接近/低于 1、inter-arrival 近指数(Poisson-like)[实测,ServeGen §5.2] | [MLPerf DS-R1](https://mlcommons.org/2025/09/deepseek-inference-5-1/) · [ServeGen](https://arxiv.org/pdf/2505.09999) · [SAECache](https://arxiv.org/html/2605.18825v1) · [Epoch](https://epoch.ai/data-insights/output-length) |
| **agentic**(coding/tool-loop) | **巨前缀**:系统提示 **~20K+**(Claude Code)/ 工具定义占大头 [实测,LMCache];累积上下文到第 30 轮 **~80K、max >180K** [实测,vLLM×Mooncake];in:out ≈ **131:1** [实测] | **每轮短** decode(几百~几千 token);每轮上下文增长 **~2,242 tok** [实测,vLLM×Mooncake] | **复用金矿**:每轮前缀(系统提示+工具定义+历史)复用,**整体命中 92–94%**(分阶段 92%→98%)[实测];**陷阱:动态工具结果不该缓存**(`docs/07` Don't Break the Cache) | **极高**;Codex/SWE trace 中位 **33 轮/trace**;轮间空闲 **中位 5.2s / P99 81.4s** [实测];SAECache 实测 agentic inter-turn **P50 8.5s**(比聊天突发一个量级)[实测] | session 内突发、轮间短空闲;**轮间空闲是 LRU 误驱逐窗口** | [vLLM×Mooncake](https://vllm.ai/blog/2026-05-06-mooncake-store) · [LMCache Claude Code](https://blog.lmcache.ai/en/2025/12/23/context-engineering-reuse-pattern-under-the-hood-of-claude-code/) · Mooncake `toolagent` trace · [SAECache](https://arxiv.org/html/2605.18825v1) |
| **多轮对话**(chat) | ShareGPT 均值 **161**(16–3.2K)[实测];Azure-conv 中位 **~1,020** [实测,Splitwise] | ShareGPT 均值 **338**(19–991)[实测];Azure-conv 中位 **129**、近**双峰** [实测];BurstGPT chat 输出近高斯、比 Llama 短 [实测] | **会话内逐轮增长前缀**(第 N+1 轮吃前 N 轮全文,字节一致);跨会话靠共享系统提示 | **中**;to-C 复用 **80% <10min**、P50 inter-turn **~110s** [实测,SAECache];多轮触发下一轮概率 **mean 0.8** vs 单轮 0.5 [实测,Wild] | **周期性突发**:工作时间峰、夜间/周末谷;Gamma 刻画短期突发 [实测,BurstGPT];35% 会话仅 1 轮、中位 2 轮、75% ≤4 轮 [实测] | [Splitwise/Azure](https://arxiv.org/html/2311.18677v2) · [BurstGPT](https://arxiv.org/html/2401.17644v4) · [SAECache](https://arxiv.org/html/2605.18825v1) · [Wild](https://arxiv.org/html/2506.02634v5) |
| **RAG** | **大**:多检索 chunk(常 512-tok/chunk)+ query;BookCorpus 类长文均值 **~1,952**(18–461K)[实测,长输入参照] | 中等(答案,非 thinking) | **前缀复用结构性破裂**:检索 chunk **每次顺序不同**,破坏简单 prefix;**chunk 本身热门**(热文档跨 query 复用)但**位置不固定** [实测,CacheBlend];需 **position-independent 复用**(重算 10–15% HKVD token)才能用 [实测] | chunk 级复用率高但**朴素 prefix-caching 抓不到**;CacheBlend 实测 TTFT **−2.2~3.3×** | 跟随上层应用(chat/agent)的到达;**本身无独立到达 trace** | [CacheBlend EuroSys'25](https://arxiv.org/abs/2405.16444) · [LMCache CacheBlend](https://blog.lmcache.ai/2025-03-31-eurosys/) · [CacheClip](https://arxiv.org/html/2510.10129v1) |
| **长上下文**(长文档 QA) | **极大**:Mooncake input 直到 **126k** [实测];BookCorpus 单条到 **461K** [实测,长尾参照] | 中等 | 单大前缀**会话内复用**;跨会话取决于文档是否共享 | 取决于是否同文档多问;长前缀 prefill **主导 TTFT** | 通常低 QPS、长尾延迟 | Mooncake trace · `docs/07` Strata(2508.18572) |

**画像三条硬结论(直接影响 Foretoken):**

1. **reasoning ≠ 长 chat。** output 长 4–8× 且**几乎不可 KV 复用**(thinking 是一次性的)→ **reasoning 把
   价值从"KV 复用"转移到"decode 阶段(MTP/投机解码 + TPOT)"**。`docs/06` 假设 **CoT 复用率 ~2% 已被
   SAECache Table 1 实测坐实(2.2%,intra 4.2% / inter 0.0%)**,无需修正,可直接引用为一手支撑。
2. **agentic 是 KV 复用价值最高、也最容易被污染的负载。** 92–98% 命中靠巨前缀,但**动态工具结果**若被
   当前缀缓存反增延迟(`docs/07`)→ **这正是 value-aware 区别于纯 prefix-caching 的差异化战场。**
3. **RAG 的复用价值真实存在但被前缀假设挡住。** 不做 position-independent(CacheBlend 路线)就只能划
   out-of-scope —— **与 `docs/07` 现状一致,本篇为该决策补上量化依据(chunk 热但位置随机)。**

---

## 3. 公开 trace 对比表

| trace | 规模 / 时长 | input/output(实测) | 时间戳 | 前缀复用 hash | 突发刻画 | 主用途 | 来源 |
|---|---|---|---|---|---|---|---|
| **Mooncake**(★) | 1h × 三子集 | in 891–126k | ✅ ms | ✅ **512-block hash_ids** | session 节奏 | 多轮/agentic/长上下文复用骨架 | [2407.00079](https://arxiv.org/html/2407.00079v1) |
| **vLLM×Mooncake Codex/SWE** | **610 traces、中位 33 轮** | ctx 至 80K/turn30、max 180K;in:out 131:1 | ✅ 轮间 **5.2s 中位 / 81.4s P99** | ✅ 块 hash | 轮间空闲 | **agentic 时间维度建模** | [vLLM blog](https://vllm.ai/blog/2026-05-06-mooncake-store) |
| **BurstGPT** | **10.31M 请求 / 213 天** | 请求长 Zipf;输出近高斯 | ✅ + session | ❌ | ✅ **Gamma,CV=1/√α**;conv 周期/API 非周期 | 真实突发 + 会话边界 | [2401.17644](https://arxiv.org/html/2401.17644v4) |
| **Azure 2023**(Splitwise) | 2 服务,2023-11-11 | **coding 中位 1500/13;conv 1020/129** | ✅ 到达 | ❌ | 短窗重尾 | 到达 + 长度重尾(in:out 极不均) | [2311.18677](https://arxiv.org/html/2311.18677v2) |
| **Azure 2024**(DynamoLLM) | 1 周(5/10–19),code+conv | 字段 `TIMESTAMP/ContextTokens/GeneratedTokens` | ✅ | ❌ | 短窗重尾 | 到达 + 长度建模 | [Azure repo](https://github.com/Azure/AzurePublicDataset/blob/master/AzureLLMInferenceDataset2024.md) |
| **LMSYS-Chat-1M / ShareGPT** | 1M 对话 | ShareGPT in 161 / out 338 | ❌ | ❌(真实多轮→可自构前缀) | 无 | 真实语义多轮**内容** | [2309.11998](https://arxiv.org/abs/2309.11998) |
| **ServeGen**(生成器,非 trace) | 生产派生模型 | reason≈4×answer;client 极偏(R 25,913 client 中 top10=50%) | 生成 | 生成 | ✅ CV 建模、reasoning Poisson-like | **防"朴素拼接误判 50% 容量"** | [2505.09999](https://arxiv.org/pdf/2505.09999) |
| KVCache-in-Wild(画像,非可下载 trace) | 大云厂双 trace | A 单轮973/多轮5953 | — | — | head-user 偏斜 | **复用率/时距画像基准** | [2506.02634](https://arxiv.org/html/2506.02634v5) |

**对比要点**:① **只有 Mooncake 系列开箱带前缀复用 hash** —— 仍是缝合法骨架的唯一选择(`docs/07`)。
② **reasoning 没有公开带 hash 的到达 trace** —— 只能用 MLPerf DS-R1 长度分布 + ServeGen 的 Poisson-like
到达**合成**(见 §7)。③ **Azure/BurstGPT 提供真实到达但无复用 hash**,用于"注入真实突发"而非复用评测
(与 `docs/04` 一致)。

---

## 4. 到达 / burst 模式

**单类负载到达不是简单泊松:**
- **多轮对话 = 周期性突发**:BurstGPT 实测 conversation 服务**工作时间峰、夜间/周末谷**,短期由 **Gamma
  分布**刻画突发,**CV = 1/√α**(α 越小越突发);API 服务到达**非周期、稠密、不可预测**
  ([§5, 2401.17644](https://arxiv.org/html/2401.17644v4))。会话结构:35% 仅 1 轮、中位 2 轮、75% ≤4 轮。
- **reasoning = 反而平稳**:ServeGen 实测 reasoning 负载 **CV 接近/低于 1、inter-arrival 近指数
  (Poisson-like),明显比通用语言负载不突发**([§5.2, 2505.09999](https://arxiv.org/pdf/2505.09999))。
  **反直觉但重要:reasoning 突发性低,瓶颈在长 decode 而非到达抖动。**
- **agentic = 会话内突发 + 轮间短空闲**:轮间 **5.2s 中位 / 81.4s P99**(vLLM×Mooncake)、SAECache agentic
  inter-turn P50 **8.5s**(比 chat 110s 突发一个量级)。**轮间空闲正是 LRU 会误驱逐高价值前缀的窗口**
  —— `docs/04` 注入轮间空闲的依据。

**跨节点 / 多区域(新增,补 `docs/04` 陷阱 #8)**:多区域部署下各区域**昼夜错峰**,跨区聚合可提利用率;
但**前缀亲和路由必须跨区协调**否则命中崩塌。代表工作:**SkyWalker**(用 user/session ID 一致性哈希 +
多区前缀 trie 保 KV 局部性)、**GORGO**(联合优化算力 + 网络延迟 + 前缀命中以最小化 TTFT)
([SkyWalker 2505.24095](https://arxiv.org/pdf/2505.24095) · [GORGO 2602.11688](https://arxiv.org/pdf/2602.11688))。
→ **对 Foretoken 的含义**:若声明多节点 goodput,必须固定实例数 + 报路由策略(`docs/04` #8),且承认
**跨区缓存局部性是独立变量**,不能用单区数外推。

---

## 5. 多维对标方法(vs llm-d / Dynamo / AIBrix 要对齐什么)

**关键发现:三大编排栈对标口径已收敛到同一指标 —— goodput under (TTFT, TPOT)。** 这对 Foretoken 是好
消息:**沿用 `docs/04` 的 goodput 定义即天然可比。**

| 系统 | 头条指标 | SLO 维度 | 负载 / harness | 与 Foretoken 对齐点 |
|---|---|---|---|---|
| **NVIDIA Dynamo** | **goodput**(满足 TTFT/TPOT 界内完成的请求数,非裸吞吐) | TTFT + TPOT | GenAI-Perf/AIPerf;`mooncake_trace` | goodput 定义 + AIPerf 已支持 mooncake trace(`docs/07`) |
| **AIBrix** | **goodput**(TTFT/TPOT 约束下最小化每查询成本) | TTFT + TPOT | 端到端 benchmark | 每查询成本 ≈ 我们的 **goodput-per-GPU-byte** 卖点 |
| **llm-d** | predicted-latency 路由 | **每服务器预测 TTFT+TPOT**(XGBoost),算 SLO headroom | inference-perf | TTFT/TPOT headroom 调度 ↔ 我们的 value-aware 驱逐 |

**要做到可比,必须对齐的负载维度(对标 checklist):**
1. **同一 trace + 同一长度分布**(否则 §1 的 50% 容量误判);**报到达过程**(自计时 vs 固定 QPS)。
2. **同一 SLO 口径**:TTFT 与 TPOT **分开报 P50/P90/P99**,goodput = 联合达标率 ≥90% 下的 req/s
   (`docs/04`、`docs/07`)。**别用单一平均延迟对标**(三家都不用)。
3. **同一容量区间**:在 **<2×HBM** 打(`docs/07` 硬约束),否则 LRU 已够好。
4. **同一并行/CUDA-graph/warmup**(`docs/04` 各 arm 一致)。
5. **声明是否 disaggregated**:三家都在做 PD 分离;**Foretoken 若单体则明确标注**,不与分离结果直接比。

**来源**:[llm-d predicted-latency](https://llm-d.ai/blog/predicted-latency-based-scheduling-for-llms) ·
[AIBrix/Dynamo 对比](https://pacoxu.wordpress.com/2025/12/03/how-to-choose-the-inference-orchestration-solution-aibrix-or-kthena-or-dynamo/) ·
Dynamo AIPerf(`docs/07`)。

---

## 6. 租户异质 SLO(多租户混部)

**核心建模:SLO 不是单一阈值,而是按"应用类 × 处理阶段"分层。** SLOs-Serve(MLSys'25)给出可直接抄的
**两档 + 分阶段**模板([2504.08784](https://arxiv.org/html/2504.08784v1)):

| 档位 | TTFT | TPOT | 典型类 |
|---|---|---|---|
| **Tight** | **3× 基线松弛** | **50 ms** | 摘要(prefill 重)、代码(per-token 快) |
| **Loose** | **5× 基线松弛** | **100 ms** | 聊天(读速即可) |

**关键:同一请求不同阶段 SLO 不同**(SLOs-Serve 实测各类长度,可直接用作合成长度):
- **reasoning**:thinking 阶段 TTFT 紧、final answer decode 可松(prompt 127 / thinking **4,693** / response 803)。
- **agentic**:tool-loop 的 prefill/工具往返紧、最终 decode 松(prompt 690 / out 116)。
- **chat**:763 prompt / 266 out,两端都松。**code**:847 prompt / **26 out**(per-token 紧)。

**多租户混部的调度含义**:利用 SLO 异质性做**准入 + 队列 + 批选择**(Scorpio:TTFT Guard 最早截止优先 +
拒绝不可达请求,TPOT Guard 信用制批处理,**goodput ↑14.4×、SLO 达标 ↑46.5%**,
[2505.23022](https://arxiv.org/html/2505.23022v1));Nitsum 处理"交互式 + 后台批"分层 SLO 同部署
([2605.05467](https://arxiv.org/html/2605.05467))。

**对 Foretoken 的含义**:① 缓存价值函数应**按租户/类分键**(`docs/06` 已有:跨租户复用 ≈0 → 按租户对
流行度分键);② **value-aware 驱逐应感知 SLO 档位** —— 紧 TTFT 租户的前缀复用价值更高(命中省的是它最
吃紧的 TTFT);③ 评测应**至少混两档租户**(交互式 chat + reasoning 批),否则单租户测不出 value-aware 在
SLO 异质下的优势。

---

## 7. 对 Foretoken 缝合法的具体修订(改 `docs/07 §6.5`)

`docs/07` 缝合法 = **Mooncake 骨架(时序/并发/轮次/复用)+ prompt pool(内容)+ 真模型生成**。本篇画像
要求**四处修订/新增**:

1. **新增 reasoning arm(原缝合法缺)**。Mooncake 三子集**不含 reasoning**;reasoning 无公开带 hash 的
   trace。**做法**:用 **MLPerf DS-R1 长度分布(in ~800 / out ~3,880, max 20K)** + **ServeGen 的
   Poisson-like 到达(CV≈1,近指数 inter-arrival)** 合成 reasoning 到达;prompt pool 取 **AIME/MATH500/
   GPQA-Diamond/MMLU-Pro/LiveCodeBench**(MLPerf DS-R1 同款,现成);**真模型(GLM-think 类)现场生成
   thinking + answer**。**复用结构按 SAECache 设 CoT 权重 ≈0.02(立即驱逐)**,验证 value 函数不会错误
   保留一次性 thinking。**reasoning arm 主要压 MTP/TPOT,不是 KV 复用** —— 评测重点放 decode 加速。

2. **agentic arm 用真实数字校准而非估**。把每轮上下文增长设到 **~2,242 tok/turn**、会话 **~33 轮**、
   轮间空闲 **5.2s 中位 / 81.4s P99**、目标命中 **92–94%**(vLLM×Mooncake 实测);系统提示+工具定义固定
   前缀设 **~12–20K**(`docs/07` 已写 12k;Claude Code 实测 20K+ → **用 12–20K 区间**)。**必加污染对照组**:
   注入动态工具结果,验证 value 函数降权(`docs/07` Don't Break the Cache),这是 agentic 的差异化证据。

3. **多轮 chat arm 的时距校准**。inter-turn 用 **SAECache log-normal(chat μ=4.82, σ=1.25 → P50≈110s)**
   而非随手设;会话长度按 BurstGPT(中位 2 轮、75% ≤4 轮)。**让轮间空闲真实**,才逼出 LRU 误驱逐。

4. **注入真实突发**。在 Mooncake 自计时回放上,**叠加 BurstGPT 的 Gamma 突发(CV=1/√α)** 做 chat/agentic
   的到达抖动;**reasoning arm 反而用近泊松**(ServeGen)。**别给所有负载用同一个到达过程** —— 这是
   `docs/07` 现状的隐患(默认全用 Mooncake 节奏,reasoning 应更平稳、agentic 更突发)。

5. **RAG 维持 out-of-scope,但补量化依据**。`docs/07` 已标 RAG 最脆;本篇补:**chunk 热但位置随机**,
   朴素 prefix-caching 抓不到,需 CacheBlend 式重算 10–15% HKVD token([CacheBlend](https://blog.lmcache.ai/2025-03-31-eurosys/))。
   **若将来要纳入,做"同文档集不同顺序"对照暴露破裂**(`docs/07` 已写),并明确这是 position-independent
   复用路线、不在当前 value-aware-LRU 范围内。

6. **评测至少混两档租户 SLO**(§6)。在缝合负载上**同时跑交互式 chat(Loose 5×/100ms)+ reasoning 批
   (thinking 紧 / answer 松)**,goodput 按 SLOs-Serve 分档算,才能体现 value-aware 在 SLO 异质下的增益。

---

## 8. 开放问题

1. **reasoning 的 KV 真零复用吗?** SAECache 测 CoT inter-conv 复用 0.0%,但**前缀(题目+system)仍可复用**;
   且新兴工作(2601.20326 "Utilizing KV Cache for Sampling and Reasoning")探索 **thinking 内部的 KV 复用**
   (多采样/自一致性间)。**Foretoken 是否要把 reasoning 的 prefix 复用 vs thinking 不复用拆开建模?** 待定。
2. **reasoning 长度分布是单源(MLPerf)。** mean in 800 / out 3,880 仅 MLPerf DS-R1 一个数据集口径;不同
   模型(GLM/o1)、不同难度的 thinking 长度差异大(Epoch 只给"~8× 趋势")。**[待核实]**:GLM-think 在
   AIME/GPQA 上的实测长度分布,建议自测一遍再固化进合成器。
3. **agentic 系统提示规模快速变化。** Claude Code 20K+(2025-12)vs `docs/07` 12k —— **前缀大小是移动靶**,
   评测应把"固定前缀大小"作为一个显式扫描轴而非定值。
4. **跨区复用局部性如何评?** SkyWalker/GORGO 是多区路由问题;**单机 value-aware 驱逐 + 跨区路由是否应联合
   评测**,还是分层处理?当前 `docs/04` 仅"固定实例 + 报路由",未深入。
5. **租户异质 SLO 下 value-aware 的增益能否归因到"对紧 SLO 租户的命中"?** 需要**按租户分层报 goodput**,
   而非聚合 —— 否则 value-aware 的优势可能被聚合平均掉。
6. **MTP 接受率随负载类型变化吗?** reasoning(结构化 CoT)vs chat(自由文本)vs code 的 MTP 接受率差异
   未见公开实测;`docs/05`/`docs/07` 已要求真模型真内容测,但**应把"按负载类报接受率"列为显式产出**。

---

## 来源(一手 URL)

- KVCache in the Wild(ATC'25):https://arxiv.org/html/2506.02634v5 —— 复用率/时距/单轮 vs 多轮/偏斜画像
- SAECache(2605.18825):https://arxiv.org/html/2605.18825v1 —— **token 类型复用 System 92.3% / CoT 2.2% / 42–756× 跨度**;inter-turn log-normal(chat P50 110s / agentic 8.5s)
- MLPerf Inference v5.1 DeepSeek-R1:https://mlcommons.org/2025/09/deepseek-inference-5-1/ —— **reasoning 实测 mean in 800 / out 3,880 / max 20K**;AIME/MATH500/GPQA-D/MMLU-Pro/LiveCodeBench
- ServeGen(NSDI'26,2505.09999):https://arxiv.org/pdf/2505.09999 —— reason≈4×answer;reasoning Poisson-like(CV≈1);**朴素拼接 50% 容量误判**;client 偏斜
- Epoch AI(输出长度趋势):https://epoch.ai/data-insights/output-length —— reasoning ~8× 长、5×/年增长
- vLLM×Mooncake agentic blog:https://vllm.ai/blog/2026-05-06-mooncake-store —— **Codex/SWE 610 trace、中位 33 轮、ctx→80K/max180K、+2,242 tok/turn、in:out 131:1、命中 94.2%、轮间 5.2s/81.4s**
- LMCache Claude Code 上下文工程:https://blog.lmcache.ai/en/2025/12/23/context-engineering-reuse-pattern-under-the-hood-of-claude-code/ —— 系统提示 **20K+**、18 工具、分阶段前缀复用 92%→98%
- BurstGPT(KDD'25,2401.17644):https://arxiv.org/html/2401.17644v4 —— **10.31M/213天**;Gamma 突发 CV=1/√α;conv 周期/API 非周期;会话 35%/中位2/75%≤4
- Splitwise(ISCA'24,2311.18677):https://arxiv.org/html/2311.18677v2 —— **Azure coding 中位 1500/13、conv 1020/129**
- Azure Public Dataset(2024):https://github.com/Azure/AzurePublicDataset/blob/master/AzureLLMInferenceDataset2024.md —— 字段 + DynamoLLM 来源
- LMSYS-Chat-1M(2309.11998):https://arxiv.org/abs/2309.11998 —— 真实多轮内容;ShareGPT in 161/out 338(经 Wild 引用)
- CacheBlend(EuroSys'25,2405.16444):https://arxiv.org/abs/2405.16444 / https://blog.lmcache.ai/2025-03-31-eurosys/ —— RAG position-independent;重算 **10–15% HKVD**;TTFT −2.2~3.3×
- SLOs-Serve(MLSys'25,2504.08784):https://arxiv.org/html/2504.08784v1 —— **分阶段两档 SLO(Tight 3×/50ms,Loose 5×/100ms)**;各类长度(reasoning 127/4693/803 等)
- Scorpio(2505.23022):https://arxiv.org/html/2505.23022v1 —— 异质 SLO 调度,goodput ↑14.4×
- Nitsum(2605.05467):https://arxiv.org/html/2605.05467 —— 交互式 + 后台批分层 SLO 同部署
- llm-d predicted-latency:https://llm-d.ai/blog/predicted-latency-based-scheduling-for-llms —— XGBoost 预测 TTFT/TPOT headroom 路由
- AIBrix/Dynamo 对标:https://pacoxu.wordpress.com/2025/12/03/how-to-choose-the-inference-orchestration-solution-aibrix-or-kthena-or-dynamo/ —— 三家均 goodput under TTFT/TPOT
- SkyWalker(2505.24095):https://arxiv.org/pdf/2505.24095 / GORGO(2602.11688):https://arxiv.org/pdf/2602.11688 —— 跨区前缀局部性 + 昼夜错峰路由

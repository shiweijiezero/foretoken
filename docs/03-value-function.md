# 03 —— 价值函数设计 + 拥挤的前沿(定位现实核查)

范围:跨请求的**前缀缓存块**驱逐/准入(radix/块层),**不是**序列内 attention 稀疏化的
token 丢弃(H2O/SnapKV/Ada-KV —— 不同的问题、不同的正确性)。价值目标:
`value(block) = P(reuse) × recompute_cost × SLO_value`。

> **⚠️ 现实核查(承重的发现):截至 2025 末/2026,这个领域已经拥挤。** 一个朴素的"价值感知
> KV 驱逐胜过 LRU"**站不住脚** —— 它只比 GDSF(1998)/ LHD(NSDI'18)略微领先,而且 LLM
> 专用的实例已经存在。可守的新意很狭窄,难点在 `P(reuse)` 估计器。见 §3–§6。

## 1. 经典理论 → 采用 LHD 形式
| 策略 | 价值函数 | 备注 |
|---|---|---|
| Belady / **PFOO**(PoMACS'18) | 最远未来 / 变长的离线最优 | **正确的离线 oracle**(KV 块是变长的;用 PFOO,而非朴素 Belady) |
| GDS / **GDSF**(USITS'97/'98) | `Clock + Freq·cost/Size` | 启发式的先祖:成本+大小+频率,经 aging 体现近因;**没有驻留分母** |
| **LHD**(NSDI'18) | **`P(hit) / (size × E[residency])`**(以 age + class 为条件) | 有原则的"每字节秒期望价值";天然可接纳一个成本/价值项 —— **规范的目标** |
| TinyLFU / W-TinyLFU | 当且仅当 CMS-freq(候选) > freq(受害者) 时准入 | 准入原语 |
| S3FIFO(SOSP'23)/ SIEVE(NSDI'24) | 1 bit 频率 + 快速降级 | O(1),对 Zipf 鲁棒;用来混合的鲁棒回退 |

## 2. 学习型缓存 → 热路径解析式 + 后台学习
LRB(NSDI'20,模仿 Belady)在每块粒度上太重;**GL-Cache(FAST'23):在分组(GROUP)层面
学习 → 228× 于 LRB 的吞吐**。⟹ 工程范式:**热路径 = 在廉价的节点特征上做闭式密度 + LHD 的
64 对象随机采样排序**(无全局排序、无每块 NN);**学习放在后台循环**(按类的分布参数,而非
每次访问做推理)。

## 3. 拥挤的前沿(KV 专用,2024–2026)—— 已经存在的东西
- **SAECache**(arXiv 2605.18825,2026):**实质上就是针对 KV 前缀块的 LHD 每时间命中密度
  策略** —— `P(b)=α_q·w_τ(b)·p_q(b)/Δt_b`,带学习到的 token 类型权重(系统提示词 92% 复用
  vs CoT 2%)+ log-normal 生存。**仅 P(reuse)**(无成本/SLO);在单轮上回归 12–34%。
- **LPC**(NeurIPS'25):学习型的会话续接 P(reuse);相比 LRU 缓存小 18–47%。仅 P(reuse)。
- **vLLM T-LRU RFC #37823(开放,仅参考实现分支、无已合并 PR)** —— *关键的竞争信号*:vLLM
  社区正在构建 **尾延迟 / 会话长度感知**的 TTFT-SLO 前缀驱逐(`B=max(0,H+Q̂−ξ)`;CLI
  `--tlru-xi-tokens`/`--tlru-qhat-tokens`;对应 NeurIPS'25 Tail-Optimized Caching 2510.15152)。
  **注意:它本身不含重算成本项**——重算成本那条是另一个 *已关闭 / not-planned* 的 RFC #23641
  (Frequency+Cost Eviction)。它按会话而非每字节密度;Q̂ 是启发式的,不是经校准的 P(reuse)。
- **TensorRT-LLM** 已交付优先级+retention 的 KV 驱逐(SLO 作为一个*手动旋钮*)。
- OrbitFlow(按请求的 ILP 层保留,非每字节)、TokenCake(agent 关键性)、EVICPRESS(统一
  效用但用于压缩)、KVFlow(工作流的"距执行步数",基于规则)。

## 4. 已落地的策略 —— 确切的缺口
所有策略都按**近因 ± 频率 ± 一个手动优先级**排序:vLLM = 纯 LRU;SGLang radix =
LRU/LFU/FIFO/MRU/Priority(`len(node.key)` **可用但未用**;`node.priority` 是一个外部 int
钩子);HiCache = 复用计数的写穿准入(类似 TinyLFU-doorkeeper)但近因驱逐;LMCache =
LRU/LFU/FIFO(+ARC);AIBrix = S3FIFO;Mooncake = LRU/LFU/LengthAware。**没有一个计算每字节
价值;有 size 可用之处也未用;重算成本从未被建模;SLO 只是手动;P(reuse) 至多 1 bit/2 次
命中。**

## 5. 可守的新意(就这么窄地陈述它)
> **一个单一的每字节期望 goodput 密度,把经校准的 `P(reuse)`(持久化感知)× 物理上有依据的
> `重算 vs 重载成本` × `SLO/租户价值` 融合进单一排序,同时治理 驱逐 AND 跨层准入 —— 并可
> 证明地退化到 LHD、GDSF、T-LRU 作为特例。** 没有任何前作融合了这三者:SAECache/LPC 省略了
> 成本+SLO(且在单轮上回归);T-LRU/TRT-LLM 省略了经校准的复用 + 每字节密度;OrbitFlow 是
> 按请求而非每字节;EVICPRESS 的效用是为压缩服务的。统一 + 这个 subsumption(包含)证明 +
> 层感知准入才是贡献;每字节价值这个想法本身**并不**新。

## 6. 规范目标
**`Value(b) = [ P(reuse|Δt,class) × recompute_cost(b) × SLO_value(b) ] / [ size(b) × E[residency|class] ]`**
—— 按升序驱逐;当且仅当 Value 超过被挤出字节的边际价值时准入到某一层(TinyLFU 式,价值
泛化)。退化为 **LHD**(cost=SLO=1)、**GDSF**(去掉驻留分母)、**T-LRU**(class=会话,在
TTFT 预算处把 SLO 二值化)。`E[residency]` 分母(LHD 的区分点,GDSF 缺它)阻止大/长块占着
茅坑 —— 这很重要,因为 KV 块又大又变长。
- `recompute_cost = min(prefix_len·c_recompute, bytes/B_PCIe + lat)` —— 即 **PCIe↔HBM 权衡**
  (重载 2–10 ms;只有在低于某个临界的 已缓存/prefill 比率时,卸载才有帮助),**依赖于层
  和负载**。
- `SLO_value = w(tenant_priority)·g(deadline_slack)`,按一个块所服务的请求取 max 加权。

## 7. 最难的子问题(已确认)+ 缓解
**小时/天级持久化下的 `P(reuse)` 估计器**(共享系统提示词、RAG 分块、回头的会话)——
SAECache 只对短多轮建模,且在单轮上回归;不存在经校准的非平稳长尾模型。危险:误校准 →
把冷字节钉住,或驱逐很快会被复用的共享前缀(重算风暴)。从第一天就内建:(1)**竞争安全**
—— 把学习到的密度与 S3FIFO/LRU 混合,使最坏情况保持在 ML 增强的 `min(2+4√(η/OPT),4H(k))`
界内;(2)**带不确定性的按类生存拟合**(样本少时收缩到近因);(3)**对标 PFOO 评测**
(变长最优),而非朴素 Belady。

## 8. 对 MVP 门槛的影响(锐化 `04`)
LRU 如今是一个**弱**基线。诚实的门槛必须击败 LRU,**并且**逼近/击败真正的竞争者
(**T-LRU、SAECache**),并弥合 **LRU→PFOO** 差距的一大部分 —— 增益集中在长复用距离桶,
那里经校准的长视野 `P(reuse)` 才是差异点。(此处的 vLLM 集成接缝在
`02-vllm-kv-hookpoints.md`;本文是引擎无关的价值函数理论。)

## 来源
LHD NSDI'18 · GDSF USITS'97 · TinyLFU 1512.00727 · S3FIFO SOSP'23 · SIEVE NSDI'24 · LRB NSDI'20 ·
GL-Cache FAST'23 · PFOO PoMACS'18 · ML-augmented caching 1910.12172 · **SAECache 2605.18825** ·
**LPC NeurIPS'25** · **vLLM T-LRU RFC #37823** · TRT-LLM priority eviction (NVIDIA) · KVFlow 2507.07400 ·
OrbitFlow 2601.10729 · TokenCake 2510.18586 · EVICPRESS 2512.14946 · SmartSpec 2406.14066 ·
KV-offload bottleneck 2601.19910.

# 03 — 价值函数设计与领域现状

范围:跨请求的**前缀缓存块**驱逐/准入(radix/块层),不是序列内 attention 稀疏化的 token
丢弃(H2O/SnapKV/Ada-KV,属于不同的问题与不同的正确性约束)。价值目标:
`value(block) = P(reuse) × recompute_cost × SLO_value`。

## 1. 领域现状

截至 2025 末 / 2026,该领域已较为拥挤。"价值感知 KV 驱逐胜过 LRU"本身不足以构成贡献:它相
对 GDSF(1998)/ LHD(NSDI'18)仅略有领先,且已存在 LLM 专用实例。可守的新意范围狭窄,主要难
点在 `P(reuse)` 估计器。详见 §3–§6。

## 2. 经典理论与 LHD 形式

| 策略 | 价值函数 | 备注 |
|---|---|---|
| Belady / **PFOO**(PoMACS'18) | 最远未来 / 变长的离线最优 | 离线 oracle(KV 块变长;采用 PFOO,而非朴素 Belady) |
| GDS / **GDSF**(USITS'97/'98) | `Clock + Freq·cost/Size` | 成本 + 大小 + 频率,经 aging 体现近因;无驻留分母 |
| **LHD**(NSDI'18) | **`P(hit) / (size × E[residency])`**(以 age + class 为条件) | 每字节秒的期望价值;可接纳一个成本/价值项,作为规范目标 |
| TinyLFU / W-TinyLFU | 当且仅当 CMS-freq(候选) > freq(受害者) 时准入 | 准入原语 |
| S3FIFO(SOSP'23)/ SIEVE(NSDI'24) | 1 bit 频率 + 快速降级 | O(1),对 Zipf 鲁棒;用作混合的鲁棒回退 |

## 3. 学习型缓存:热路径解析式 + 后台学习

LRB(NSDI'20,模仿 Belady)在每块粒度上开销过高;GL-Cache(FAST'23)在分组(GROUP)层面学习,
吞吐为 LRB 的 228×。由此确定工程范式:**热路径 = 在廉价的节点特征上做闭式密度 + LHD 的 64
对象随机采样排序**(无全局排序、无每块 NN);**学习放在后台循环**(按类的分布参数,而非每次
访问做推理)。

## 4. KV 专用领域现状(2024–2026):已有工作

- **SAECache**(arXiv 2605.18825,2026):针对 KV 前缀块的 LHD 每时间命中密度策略 ——
  `P(b)=α_q·w_τ(b)·p_q(b)/Δt_b`,带学习到的 token 类型权重(系统提示词 92% 复用 vs CoT
  2%)+ log-normal 生存。仅含 P(reuse)(无成本/SLO);在单轮上回归 12–34%。
- **LPC**(NeurIPS'25):学习型的会话续接 P(reuse);相比 LRU 缓存小 18–47%。仅含 P(reuse)。
- **vLLM T-LRU RFC #37823**(开放,仅参考实现分支、无已合并 PR):vLLM 社区正在构建尾延迟 /
  会话长度感知的 TTFT-SLO 前缀驱逐(`B=max(0,H+Q̂−ξ)`;CLI `--tlru-xi-tokens`
  / `--tlru-qhat-tokens`;对应 NeurIPS'25 Tail-Optimized Caching 2510.15152)。其本身不含
  重算成本项 —— 重算成本对应另一个已关闭 / not-planned 的 RFC #23641(Frequency+Cost
  Eviction)。它按会话而非每字节密度;Q̂ 为启发式,非经校准的 P(reuse)。
- **TensorRT-LLM**:已交付优先级 + retention 的 KV 驱逐(SLO 作为手动旋钮)。
- OrbitFlow(按请求的 ILP 层保留,非每字节)、TokenCake(agent 关键性)、EVICPRESS(统一效用
  但用于压缩)、KVFlow(工作流的"距执行步数",基于规则)。

## 5. 已落地策略与缺口

现有策略均按近因 ± 频率 ± 手动优先级排序:vLLM = 纯 LRU;SGLang radix =
LRU/LFU/FIFO/MRU/Priority(`len(node.key)` 可用但未用;`node.priority` 为外部 int 钩子);
HiCache = 复用计数的写穿准入(类似 TinyLFU-doorkeeper)但近因驱逐;LMCache =
LRU/LFU/FIFO(+ARC);AIBrix = S3FIFO;Mooncake = LRU/LFU/LengthAware。缺口:无一计算每字节
价值;有 size 可用之处未用;重算成本从未被建模;SLO 仅手动;P(reuse) 至多 1 bit / 2 次命中。

## 6. 可守的新意

一个单一的每字节期望 goodput 密度,将经校准的 `P(reuse)`(持久化感知)× 物理上有依据的
`重算 vs 重载成本` × `SLO/租户价值` 融合进单一排序,同时治理驱逐与跨层准入,并可证明地退化
到 LHD、GDSF、T-LRU 作为特例。

无前作融合此三者:SAECache/LPC 省略成本 + SLO(且在单轮上回归);T-LRU/TRT-LLM 省略经校准的
复用 + 每字节密度;OrbitFlow 按请求而非每字节;EVICPRESS 的效用为压缩服务。贡献在于统一 +
该 subsumption(包含)证明 + 层感知准入;每字节价值这一想法本身并不新。

## 7. 规范目标

**`Value(b) = [ P(reuse|Δt,class) × recompute_cost(b) × SLO_value(b) ] / [ size(b) × E[residency|class] ]`**

按升序驱逐;当且仅当 Value 超过被挤出字节的边际价值时准入到某一层(TinyLFU 式,价值泛化)。
退化为 **LHD**(cost=SLO=1)、**GDSF**(去掉驻留分母)、**T-LRU**(class=会话,在 TTFT 预算处
把 SLO 二值化)。`E[residency]` 分母(LHD 的区分点,GDSF 缺它)阻止大/长块长期占用容量 ——
这对又大又变长的 KV 块尤为重要。

- `recompute_cost = min(prefix_len·c_recompute, bytes/B_PCIe + lat)`,即 **PCIe↔HBM 权衡**
  (重载 2–10 ms;仅在低于某个临界的已缓存/prefill 比率时卸载才有帮助),**依赖于层和负载**。
- `SLO_value = w(tenant_priority)·g(deadline_slack)`,按一个块所服务的请求取 max 加权。

## 8. 最难的子问题与缓解

**小时/天级持久化下的 `P(reuse)` 估计器**(共享系统提示词、RAG 分块、回头的会话):SAECache
仅对短多轮建模,且在单轮上回归;不存在经校准的非平稳长尾模型。风险:误校准导致把冷字节钉住,
或驱逐很快会被复用的共享前缀(重算风暴)。从第一天起内建:

1. **竞争安全**:把学习到的密度与 S3FIFO/LRU 混合,使最坏情况保持在 ML 增强的
   `min(2+4√(η/OPT),4H(k))` 界内;
2. **带不确定性的按类生存拟合**(样本少时收缩到近因);
3. **对标 PFOO 评测**(变长最优),而非朴素 Belady。

## 9. 对 MVP 门槛的影响

LRU 是一个弱基线。MVP 门槛必须击败 LRU,并且逼近/击败真正的竞争者(**T-LRU、SAECache**),
并弥合 **LRU→PFOO** 差距的一大部分;增益集中在长复用距离桶,该处经校准的长视野 `P(reuse)`
是差异点。(vLLM 集成接缝见 `02-vllm-kv-hookpoints.md`;本文是引擎无关的价值函数理论。)

## 来源

LHD NSDI'18 · GDSF USITS'97 · TinyLFU 1512.00727 · S3FIFO SOSP'23 · SIEVE NSDI'24 · LRB NSDI'20 ·
GL-Cache FAST'23 · PFOO PoMACS'18 · ML-augmented caching 1910.12172 · **SAECache 2605.18825** ·
**LPC NeurIPS'25** · **vLLM T-LRU RFC #37823** · TRT-LLM priority eviction (NVIDIA) · KVFlow 2507.07400 ·
OrbitFlow 2601.10729 · TokenCake 2510.18586 · EVICPRESS 2512.14946 · SmartSpec 2406.14066 ·
KV-offload bottleneck 2601.19910.

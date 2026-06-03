# 11 —— 统一的 goodput 控制环路(把 KV + MTP + 调度串成一个大脑)

项目新定位的灵魂:**用单一标尺 —— 每 GPU 字节秒 goodput(`docs/04`)—— 同时驱动 KV 策略
(B3/B5/B8)、MTP 投机控制(C2)、调度(D2/D4)。** 本文是最高层的架构缺口:现有设计分层
独立(KV 管自己驱逐、MTP 管自己 K、调度管自己排序),缺一个**跨层反馈大脑**。

> **范围与诚实分界:** 本文明确区分**已有工作**(附一手 URL)与**我们的假设/新意**(标 ⟦H⟧)。
> 第 ② 节盘点把"自适应投机控制 / goodput 调度 / 跨层耦合"逐一对照,核实"OSS 中尚无产品化
> goodput 感知投机控制"这一论断在 **2026 视角** 是否仍成立(结论:**部分成立 —— 见下**)。

## ① 问题:三个独立闭环 vs 一个统一闭环
今天三层各自有一个隐式控制律,**用不同的标尺、互不感知**:
- **KV 层(B3)**:按每字节价值密度排序驱逐/准入(`docs/03`)——标尺 = `value(b)/byte·s`,
  但**看不到** decode 端正在投机多深、调度队列多长。
- **MTP 层(C2)**:选 `num_speculative_tokens`(`docs/05`)——今天 vLLM 是**单一静态 int**
  (`scheduler.py:213`);自适应版本(若启)按**接受率**调,**看不到** KV 命中率/重算成本。
- **调度层(D2/D4)**:准入/排序/抢占——按 SLO slack 或 FCFS,**看不到** 投机正在吃掉多少
  显存预算、KV 卸载正在产生多少 PCIe 流量。
**症结:三个局部最优 ≠ 全局 goodput 最优。** 反例(已有文献佐证,非臆测):高 batch 时 MTP
转负(`docs/05`:Cascade 2506.20675,3 draft token → 3× 专家激活,静态 K 可慢 1.5×),
但 MTP 控制器若只看接受率(接受率仍高)不会关掉它 —— **只有 goodput 标尺**(把 batch
负载 + 专家暂存成本 + SLO 一起算)才会。**这正是统一大脑存在的理由。**

## ② 现有工作盘点(三条线 + 是否跨层)
### 2.1 自适应投机长度控制(C2 的赛道)
| 工作 | 控制信号 | 优化目标 | goodput 感知? | 载体 / 开源? |
|---|---|---|---|---|
| **SmartSpec → TurboSpec**(2406.14066,**v2 已改名**) | batch 负载 + 历史接受率(移动平均)+ 离线 profiler 性能模型 | **goodput**(成功生成 token 数)= 显式标尺 | **是(开创者)** | **on vLLM,但仍是 fork;未合入主线**(forum #485:"plans to integrate soon",截至该帖未合) |
| **SpecServe / AdaSpec**(2503.05096) | draft **置信度分数**(∝ 接受率)+ batch + 上下文长度 | **SLO 感知** + 低延迟;非纯 goodput | 部分(SLO 估计器) | on vLLM(~2000 行);**未见开源发布** |
| **AdaServe**(2501.12162,EuroSys'26) | 每请求 **SLO/延迟目标**;硬件感知构建**投机树** | 多-SLO 约束优化;**goodput ↑1.9×、SLO 违例 ↓4.3×** | 是(目标含 goodput) | 自建系统;**未见开源** |
| **Nightjar**(2512.22420,2026) | **batch size**(MAB context)+ 请求负载 + GPU 显存压力 + 接受率 | 最小化每 token 延迟(≈goodput);**MAB planner** | 是(latency/token) | **extend vLLM;未见开源**;**含 KV 耦合**(见 2.3) |
| **DSDE**(2509.01083) | 每序列 **KLD 稳定性**;异构大 batch 内每序列 K | 延迟 | 否 | 待核实 |
| vLLM **`eagle_dynamic` / DynamicProposer**(PR **#26504**) | **纯每请求接受率**对齐 `--acceptance-rate-threshold` | 把接受率维持在阈值 | **否**(非 goodput/SLO/batch) | **open PR,未合入**(2026-04 needs-rebase) |
| vLLM **DSL 早退**(RFC **#36657**,PR #35301) | **每步 batch 均值 max-softmax 置信度** < τ 早停 | 即时置信度 | **否** | **open RFC**(2026-03),未合 |
| MagicDec(2408.11049) | 上下文长度 → 内存受限区 | 长上下文吞吐 | 间接 | 论文 |

**核实结论(2026 视角):**
- **"OSS 中尚无**产品化的** goodput 感知投机控制" —— 仍基本成立,但需精确化:**
  ① **goodput 感知**的那个(TurboSpec)**确有其物,但仍是 vLLM 的 fork,未合入主线**(forum
  #485 确认"计划合并"但截至该帖未合)。② vLLM **主线**今天的两条自适应路线(`eagle_dynamic`
  #26504、DSL 早退 #36657)**都 open 未合,且都只是接受率/置信度阈值,非 goodput 感知**。
- **⟹ 必须更新 `docs/05` 的措辞**:不能再说"vLLM 里不存在自适应投机长度"——**存在 open
  PR/RFC**;准确说法是"**vLLM 主线尚无已合入的自适应投机长度,在途的两条均非 goodput 感知**"。
  C2 的可守利基从"自适应"收窄到 **"goodput 感知 + 跨层(KV/调度)耦合 + MTP-on-EP'd-MoE 专用"**。

### 2.2 goodput 感知调度(D4 的赛道)
| 工作 | goodput 怎么挂钩 | 准入/排序/抢占 | 开源 |
|---|---|---|---|
| **DistServe**(2401.09670,OSDI'24) | **定义"每 GPU goodput"= 满足 SLO 达标目标下的最大 req/s**(我们 `docs/04` 主指标的出处) | P/D 分离 + 放置搜索 | 是 |
| **Sarathi-Serve**(2403.02310,OSDI'24) | **token budget** + chunked-prefill + **stall-free**(decode 优先,prefill 切块塞进预算) | 不暂停 decode 地准入新请求 | 是(已影响 vLLM chunked-prefill) |
| **Mooncake**(2407.00079,FAST'25) | KVCache-centric;**SLO 下早拒**(过载预测) | 准入(reject)+ KV 感知路由 | 是 |
| **JITServe**(2504.20068) | 估计**抢占的 goodput 损失**,仅当净收益为正才抢占 | 抢占 | 待核实 |
| **FlowPrefill**(2602.16603,2026) | 异构 SLO 下及时抢占低优先级 prefill;算子级 prefill 抢占 | 抢占/准入 | 待核实 |
| Revisiting SLO/Goodput(2410.14257) | **反 goodput 作弊**(承诺式 deadline,禁 stalling token)——我们 `docs/04` 已采纳 | 度量方法 | 论文 |

**结论:goodput 调度本身成熟**(DistServe 给了规范定义,Sarathi 给了 token-budget 机制)——
**D4 = Extend,复用其目标定义,不重造**;我们的增量在**把投机/KV 的成本回灌进同一个 goodput
目标**(下节)。

### 2.3 跨层联合优化 —— 关键问题:有没有人把 KV 与投机耦合?
| 工作 | 耦合了什么 | 是否 = 我们要的 KV-命中↔投机 耦合? |
|---|---|---|
| **Nightjar**(2512.22420) | **投机状态 ↔ 显存**:"Squeeze/Expand" 在 **draft 模型权重 vs KV cache** 间动态重分显存;关投机时把 draft 卸到 CPU 给 KV 腾位 | **否** —— 是 *draft 权重占用* vs KV,**不是** *前缀命中率* → 投机激进度 |
| **TransKV**(techrxiv 2026) | **未提交的 draft KV** ↔ paged KV 内存 / token-budget 调度(事务式 KV,分离稳定/投机 buffer) | **否** —— 是 draft KV 的**内存分配正确性**(正是我们 `docs/05` 的"draft KV 临时标记"约束),非命中率耦合 |
| vLLM lookahead 预留(`scheduler.py:705`) | 调度器为 K+1 步**多预留 KV 块**,不够则抢占 | **否** —— 机械预留,无价值/goodput 决策 |
| **EVICPRESS**(2512.14946) | **KV 压缩 ↔ 驱逐**统一效用函数(质量+延迟) | **否** —— KV 层内部联合,**不含投机** |
| **MagicDec**(2408.11049) | 上下文长度 → 内存受限 → 投机有利(长上下文翻正) | **半个** —— 揭示了"KV 重(长上下文/内存受限)时投机更值"这一**物理直觉**,但**未**把 *前缀命中率* 作为控制信号,也未做闭环控制器 |
| CacheBlend / 部分复用(`docs/03 B4`) | 非前缀 KV 复用 | 否(与投机无关) |

> **承重结论 ——「KV 命中率/重算成本 ↔ 投机长度」的耦合,在已发表工作中是空白。**
> 已有的全部"跨层"工作耦合的是 **(a) 投机 ↔ 显存占用**(Nightjar、TransKV、lookahead 预留)
> 或 **(b) KV 压缩 ↔ 驱逐**(EVICPRESS)。**没有任何工作把"前缀缓存命中率 / prefill 重算
> 成本"当作驱动投机激进度的信号**,反之亦然。MagicDec 给了物理直觉(KV 重 → 内存受限 →
> 投机更值)但**不是闭环控制律**,且用的是上下文长度而非命中率。⟦H⟧ **这是 Foretoken 可守的
> 新意空位**(见 ⑥)。**(注:多为臆测之处已标 ⟦H⟧;"空白"基于上述检索,拿不准的边角标"待核实"。)**

## ③ 跨层反馈环的设计草图(信号源 → 决策 → 约束)⟦H 全节为我们的设计假设⟧
一个**单一 goodput 估计器**(标尺 = `tokens·SLO_met / (GPU_bytes · s)`)被三个执行器共享。
每个执行器**把自己的成本/收益用同一货币上报**,大脑按统一边际收益分配。

**信号源(每步/每窗口采集,廉价):**
- KV 侧:**前缀命中率**、被驱逐块的重算成本(`docs/03 §6` 的 `recompute_cost`)、KV 显存占用、
  PCIe 卸载流量(`B5`)、`P(reuse)` 置信度(`docs/06`)。
- MTP 侧:**观测接受率**、当前 K、draft 步的 cudagraph 桶命中情况、MoE 专家暂存成本
  (`docs/05`:EP × draft 激活争用)。
- 调度侧:**batch size / 队列长度 / SLO slack 分布**、prefill vs decode 占比。

**决策(三个执行器,同一货币):**
1. **MTP 控制器(C2)**:`K* = argmax_K goodput(K | batch, 接受率, KV 命中, 专家成本)`,
   `受桶约束`(⑤);**可输出 K=0(关投机)**。⟦H⟧ **新耦合项:KV 命中高 → 该请求 prefill
   便宜/已省 → 它更快进入纯 decode 的内存受限区 → 给更多投机预算**(MagicDec 物理基础);
   **KV 冷 → 重算贵 → 抑制投机**(别拿稀缺算力去 draft 一个还在烧 prefill 的请求)。
2. **KV 策略(B3)**:价值密度里**注入投机的边际价值**——若一个前缀正在喂高接受率的投机,
   它的有效 `SLO_value` 应上调(留住它 = 让投机持续赚);反之被拒 draft 写的 KV **绝不**
   提升到卸载/前缀层(`docs/05` 的临时标记契约,这是**已确认的硬约束**,非假设)。
3. **调度器(D4)**:准入/抢占时**把"该请求会触发多少投机算力 + 预留多少 lookahead KV 块"
   计入它的 goodput 成本**(today vLLM 只机械预留 K+1 块,无价值判断)。

**KV ↔ MTP ↔ 调度 怎么互相感知(闭环):**
```
        ┌────────── 单一 goodput 估计器(tokens·SLO_met / GPU_byte·s)──────────┐
        │  在线性能模型(TurboSpec 式 profiler)+ 接受率 EWMA + P(reuse) 置信度  │
        └──────┬───────────────────┬────────────────────────┬──────────────────┘
   命中率/重算成本│        接受率/专家成本│           batch/队列/SLO slack│
        ┌───────▼──────┐   ┌────────▼────────┐      ┌────────▼────────┐
        │ KV 策略 B3/B5 │◄─►│  MTP 控制器 C2   │◄────►│  调度器 D4       │
        │ 驱逐/准入/TTL │   │ 选 K(含 K=0)   │      │ 准入/排序/抢占   │
        └──────────────┘   └─────────────────┘      └─────────────────┘
   契约①: draft-KV 临时(C1↔B2/B3,docs/05,硬约束)
   契约②: K 待在 cudagraph 桶内(C2↔A4,docs/05,硬约束)
   契约③: lookahead K+1 块预留 计入调度 goodput 成本(C2↔D4)
```

## ④ 全局 token / 显存预算分配
**问题:给定显存 + SLO,如何在 prefill(含 MTP draft)、decode、cache 之间动态分配。**
- **已有的形式化构件(复用):** Sarathi-Serve 的 **token budget** 是 prefill↔decode 分配的现成
  机制(2403.02310);DistServe 的 per-GPU goodput 是目标函数(2401.09670)。**但二者都没把
  cache 字节 和 投机 draft token 纳入同一个预算。**
- ⟦H⟧ **我们的扩展 = 三方预算 + 一个影子价格。** 把显存看成单一资源,prefill-chunk token、
  decode KV 块、cache 留存块、**draft KV 块** 竞争同一池子;每类的**边际 goodput / 边际字节**
  = 影子价格,大脑按影子价格相等的水位分配(经济学的等边际原理)。投机的 draft token 预算
  **不是免费的**:它既吃 FLOPs(prefill 预算)又吃 KV 块(decode 预算)——**这正是今天分层
  设计看不见的耦合成本**,放进统一预算才能被定价。
- **是否已有形式化框架?** 据检索,**prefill/decode 的二方预算有**(Sarathi、LAPS 2601.11589、
  DuetServe 2511.04791、RAPID-Serve 2601.11822),**把 cache + 投机 draft 纳入同一预算的三/四
  方形式化 —— 未见**(待核实)。这是 ④ 的潜在贡献,但**优先级低于 ③ 的耦合控制律**(预算
  框架易过度设计;先做最小可证伪的 KV↔MTP 耦合)。

## ⑤ 控制律的工程约束(cudagraph 桶 + 控制频率)—— 最尖锐的现实
- **cudagraph 桶约束(已核实,vLLM `cuda_graphs.md`):** 投机 decode 是 **UNIFORM_BATCH** 模式,
  query 长度 = **`1 + num_speculative_tokens`**。**改 K → 改 query 长度 → 改 `BatchDescriptor`
  → 触发 cudagraph 重捕获或 eager 回退**(文档明说 `BatchDescriptor` 未来才计划扩 `uniform_
  query_len` 以支持多种 uniform decode 长度)。draft 步**仅 PIECEWISE**(`gpu_model_runner.py:5856`;
  Issue #33341 在求 full-cudagraph drafter)。**⟹ 控制律不能逐请求任意改 K**:K 必须落在
  **已捕获的 `1+K` 桶集合**内(例如预捕获 K∈{0,1,2,4}),否则 eager 回退抹掉全部增益
  (`docs/05` 的"最尖锐风险"——本文核实其机理)。
- **可行的控制粒度/频率(⟦H⟧ + 工程现实):**
  - **K 的取值 = 离散小集合**(预捕获的桶),控制器在桶间切换,**不做连续 K**。
  - **控制频率 = 每调度步可改全局 K,但跨步迟滞**(避免抖动触发频繁重 dispatch);per-request
    K(#26504 路线)受限于"同 batch 内不同 query 长度 = 非 uniform → 掉出 full-cudagraph"——
    **per-request 动态 K 在 full-cudagraph 下基本不可行,这是 #26504 仍未合的工程根因之一**(待
    核实其确切阻塞点)。**保守设计:全局 K(整 batch 同 K)+ 桶约束**,比 per-request 更现实。
  - **DSL 早退(#36657)的取巧:** 早停时**零填充到固定张量形状** → 不改 `BatchDescriptor` →
    **绕开重捕获**。这是一条值得借鉴的"在固定桶内做动态深度"的工程技巧(我们 C2 可复用:
    捕获到最大 K_max,运行时早停 + zero-pad,而非真的换桶)。
  - **异步调度 × 动态 K**:MTP 已 gated-IN 异步(`docs/05`,`vllm.py:911`),但要求
    `disable_padded_drafter_batch=false`;动态 K 必须维持 padded-batch 不变量(`docs/05` 开放
    问题 #4)。

## ⑥ 可守新意 + build-vs-use
**可守新意(窄而具体,⟦H⟧):**
> **一个单一 goodput 控制器,把 KV 命中率/重算成本 作为驱动投机激进度的信号(反之亦然),
> 在 cudagraph 桶约束下 逐步选 K(含 K=0),并把投机的算力+KV 成本回灌进调度的 goodput
> 准入决策 —— 三层共用 每 GPU 字节秒 goodput 一把标尺。** 已有工作各缺一块:TurboSpec/Nightjar
> 是 goodput 感知投机但**不看 KV 命中**(只看 batch/显存占用);DistServe/Sarathi 是 goodput
> 调度但**不控投机**;EVICPRESS 统一 KV 内部但**不含投机**;TransKV/Nightjar 的 KV↔投机耦合
> 是**内存分配**而非**命中率→激进度**。**"KV 命中↔投机"这条耦合边 + 三层统一标尺,是空位。**

**诚实的护城河评估:**
- **最强的可守点**:KV↔MTP 命中率耦合(③.1 的新项)——检索下确为空白。
- **较弱**:统一 goodput 标尺本身(DistServe 已定义)、自适应投机(TurboSpec 已做,只是未上游)
  ——**这两个不是新意,是复用**。新意只在**把它们用这条新耦合边缝起来**。
- **风险**:若 ③.1 的耦合在实测中增益 <统计噪声(很可能,因为 prefill 省下的时间未必转化为
  decode 投机收益),则护城河塌缩为"又一个 goodput 投机控制器"。**必须先用最小实验证伪**(⑦)。

**build-vs-use:**
- **Use/Extend**:goodput 目标定义(DistServe)· token budget 机制(Sarathi/vLLM chunked-prefill)
  · 自适应投机的钩子(`num_lookahead_tokens` `scheduler.py:213`;若 #26504/#36657 合入则复用其
  proposer 接口)· cudagraph 桶机制(A4)· DSL 零填充早退技巧(借鉴 #36657)。
- **Build(我们的 IP)**:① **单一 goodput 估计器**(共享给三层)· ② **KV-命中 ↔ 投机 K 的
  耦合控制律**(C2 的新项,最核心)· ③ 把投机算力+lookahead KV 成本回灌进 D4 准入。
- **不重造**:drafter 本身(C1 复用原生 MTP)· goodput 调度框架(D4 Extend)· KV 价值函数
  (B3 已设计)。

## ⑦ 开放问题(最难的部分)
1. **⟦最难⟧ KV-命中↔投机 耦合到底有没有正收益?** 直觉链条"KV 命中 → prefill 便宜 → 请求更快
   进 decode 内存受限区 → 投机更值"有**多个可能断点**:prefill 省下的时间是否真转化为 decode
   阶段的投机 headroom?在高 batch 下 decode 早已 compute-bound(投机转负,`docs/05`)——此时
   KV 命中高反而**不该**加投机。**耦合的符号可能随 batch 翻转**,需要实测刻画,不能拍脑袋。
   **最小证伪实验**:固定 trace,对比"命中率门控的 K"vs"纯接受率的 K"(TurboSpec 基线),
   看 goodput 是否有 ≥统计显著 的差,且增益是否集中在**中等 batch + 高命中**象限。
2. **单一 goodput 估计器的可信度。** TurboSpec 用离线 profiler + 在线接受率;我们要再叠加 KV
   命中/重算成本/`P(reuse)` 置信度 —— **估计误差会复合**。误估 → 错误地关投机或钉死冷块。
   需要 ③ 的安全退化(估计器不确定时退回各层独立的鲁棒默认:K=静态、KV=S3FIFO,见 `docs/06`)。
3. **桶离散化 vs 控制分辨率的张力(⑤)。** 桶越多覆盖越细但捕获显存/时间越贵;桶越少控制越粗。
   `1+K` 桶集合 应取多大?per-request K 在 full-cudagraph 下是否根本不可行(#26504 阻塞根因,
   待核实)?DSL 零填充早退能否完全替代换桶?
4. **三层控制律的稳定性 / 抖动。** 三个执行器共享一个标尺 + 互相反馈 = 潜在正反馈环(关投机
   → batch 涨 → 更该关 → …;或驱逐热前缀 → 命中跌 → 抑投机 → …)。需要**迟滞 + 错频更新**
   (KV 后台守护进程节奏 vs 调度每步 vs MTP 每步),避免控制振荡。这是经典的**多时间尺度
   控制**问题,文献里**未见**在 LLM serving 三层上做过(待核实)。
5. **评测怎么把"统一控制"的增益与各层单独增益分离?** `docs/04` 的消融纪律要求"只有策略不同"——
   但统一控制天然是**交互项**。需要 2×2×2 析因设计(KV感知开/关 × 投机自适应开/关 × 耦合开/关),
   把**耦合的交互增益**单独估出来,否则无法声称"统一大脑"的贡献而非三个独立优化之和。
6. **全局预算形式化(④)值不值得做?** 三/四方影子价格框架优雅但**易过度设计**;可能 ③ 的
   pairwise 耦合控制律already 抓住 90% 收益。**默认推迟 ④,除非 ③ 的实验显示预算争用是主瓶颈。**

## 来源
**自适应投机控制:** SmartSpec/**TurboSpec** arXiv 2406.14066(https://arxiv.org/abs/2406.14066)·
SpecServe/AdaSpec 2503.05096(https://arxiv.org/html/2503.05096v1)· AdaServe 2501.12162
(https://arxiv.org/abs/2501.12162)· **Nightjar** 2512.22420(https://arxiv.org/pdf/2512.22420)·
DSDE 2509.01083(https://arxiv.org/pdf/2509.01083)· AdaSD 2512.11280(https://arxiv.org/pdf/2512.11280,
内容待核实——PDF 加密)· MagicDec 2408.11049(https://arxiv.org/pdf/2408.11049)·
vLLM DynamicProposer/`eagle_dynamic` PR #26504(https://github.com/vllm-project/vllm/pull/26504,
**open 未合**)· vLLM DSL RFC #36657(https://github.com/vllm-project/vllm/issues/36657,**open**)·
vLLM forum "Goodput Guided Speculative Decoding"(https://discuss.vllm.ai/t/goodput-guided-speculative-decoding/485,
**TurboSpec 仍 fork、计划合并**)。
**goodput 调度:** DistServe OSDI'24 2401.09670(https://arxiv.org/pdf/2401.09670)· Sarathi-Serve
OSDI'24 2403.02310(https://www.usenix.org/system/files/osdi24-agrawal.pdf)· Mooncake 2407.00079
(https://arxiv.org/pdf/2407.00079)· JITServe 2504.20068(https://arxiv.org/pdf/2504.20068)·
FlowPrefill 2602.16603(https://arxiv.org/html/2602.16603)· Revisiting SLO/Goodput 2410.14257
(https://arxiv.org/html/2410.14257v1)。
**跨层 / KV↔投机:** TransKV(https://www.techrxiv.org/doi/full/10.36227/techrxiv.177101038.80960856/v1)·
EVICPRESS 2512.14946(https://arxiv.org/pdf/2512.14946)· 全局预算:LAPS 2601.11589 · DuetServe
2511.04791 · RAPID-Serve 2601.11822。
**cudagraph 工程:** vLLM CUDA Graphs 设计文档(https://github.com/vllm-project/vllm/blob/main/docs/design/cuda_graphs.md)·
full-cudagraph drafter Issue #33341(https://github.com/vllm-project/vllm/issues/33341)· spec-decode
cudagraph 重构 PR #23679 · 本仓 `docs/05`(`file:line` 锚点)。

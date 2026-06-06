# 10 — 真实评测的实践落地缺口(benchmark↔goodput + 2026 对标 + 无损校验)

`docs/04` 给出评测的原则(为什么测 goodput、防作弊、gate-zero),`docs/07` 给出实操
(trace 选型、harness 命令、缝合法、checklist)。两份的理论框架已完整,本文补齐其中两个
实践缺口:

- **缺口 1 —— 「benchmark ↔ trace 对齐」缺少实证**:`07` 把 SWE-bench/AIME/GPQA/
  LiveCodeBench 升为验证主线,但未给出这些真实 benchmark 的请求结构画像(input/output 长度、
  系统提示重复度、可复用前缀),也未论证它们与 Mooncake 假设的复用结构是否吻合。本文 §2 用
  一手数据补齐,并指明哪个 benchmark 压 KV 复用、哪个压 MTP 接受率。
- **缺口 2 —— 「答对率评测 → 系统 goodput 评测」的改造路径缺失**:这些 benchmark 原生是精度
  评测(算 pass@1 / exact-match),不产生 TTFT/TPOT/goodput。本文 §3 给出在推理系统层
  (而非模型精度层)跑它们测 goodput 的 harness 改造路径。

§4–§8 将 `03`/`04` 停在 2024–2025 中期的竞品对标刷新到 2026(含 T-LRU RFC #37823 的
真实状态),并补充 goodput/SLO 最新定义与无损校验做法。

---

## 2. Benchmark 请求结构画像(一手数据)

> **关键事实(MLPerf Inference v5.1, 2025-09)**:MLCommons 的 DeepSeek-R1 推理 benchmark 把
> AIME(1983–2024)+ MATH500 + GPQA-Diamond + MMLU-Pro + LiveCodeBench 混成一个数据集,
> 实测平均 input = 800 tok、平均 output = 3,880 tok、最大 output = 20,000 tok(全套件最长)。
> 这是目前唯一把这四类推理 benchmark 当推理负载、并公布序列长度统计的权威源,直接坐实
> 了「推理类 benchmark 是短输入、超长输出」的画像。来源:mlcommons.org/2025/09/deepseek-inference-5-1。

| Benchmark | 规模 | 输入长度(画像) | 输出长度(画像) | 系统提示/前缀复用 | 多轮 | 适合压哪个模块 |
|---|---|---|---|---|---|---|
| **AIME** | ~30/年(83–24 全集) | 极短(单题 ~数十–数百 tok;混合集 ~800) | 极长:R1 类思维链 12k→23k tok/题(随难度;MLPerf 截至 20k) | 几乎无(单题独立) | 否 | MTP / 投机解码(长 decode,接受率主战场) |
| **GPQA-Diamond** | **448** 题(多选) | 短(题干+4 选项,~数百 tok) | 长(思维链);最终答案极短 | 无 | 否 | MTP(短入长思,decode 密集) |
| **LiveCodeBench** | **511** 题(契合后 349) | 中(竞赛题面 + 样例;无共享系统前缀,零/单样本) | 中–长(代码+思维链) | 否 | 每题独立、无共享前缀 | MTP(代码 decode);不适合压 KV 复用 |
| **SWE-bench Verified** | **500** 实例 | 极长:retrieval 版 13k–27k tok;agent 轨迹累积 20k–100k+ tok | 中(patch/工具调用,逐步) | 高:巨型稳定系统提示+工具定义(~12k 共享前缀,见 `07`)+ 逐轮累积历史 | 是(agent 多步) | KV 复用(大共享前缀 + 长会话累积,复用大头) |

**论断(benchmark↔Mooncake 对齐)**:
- **AIME / GPQA / LiveCodeBench = 短输入 + 超长输出 + 无跨题复用**。它们不匹配 Mooncake 的
  复用结构(Mooncake 的价值在 `hash_ids` 共享前缀),几乎压不出 KV 复用增益;但正中 MTP/
  投机要害,接受率与加速完全由长 decode 段的真实思维链质量决定(`docs/05`)。用它们做
  MTP 主战场。
- **SWE-bench Verified(agentic)= 巨型共享系统前缀 + 逐轮累积历史**,这正是 Mooncake
  `toolagent` 子集的复用画像(`07` §6),也是 `04` 所谓「长生命周期、时间上分隔的复用」。用它做
  KV 复用主战场;但注意 SWE-bench 原生是单轮 patch 评测,要测 KV 复用必须跑 agent 框架
  (多步工具调用)才会产生累积上下文,否则退化成一次性长 prefill,复用为零。
- **据此修订 `07` 的措辞**:`07` 把四个 benchmark 并列当「KV+MTP 通吃」的验证主线,不准确。
  正确分工是:KV 复用 → SWE-bench(agent 模式)/ 长文档多轮 QA;MTP 接受率 → AIME/GPQA/
  LiveCodeBench。这条分工写进 §7 的修订建议。

> **画像数据的精度边界**:LiveCodeBench/SWE-bench 论文未直接公布 prompt 的 token 均值;上表
> 的「极长/中」来自二手实测(R1 在 AIME 输出 12k→23k tok;SWE-bench agent 轨迹通常 <20k–30k tok,
> 极端 >100k)。MLPerf 的 800/3,880/20,000 是唯一硬数字,其余为区间画像,落地前应在自己的
> 模型(GLM-4.5-Air)上实测一遍长度分布再固化(这也是 gate-zero 的自然延伸)。

---

## 3. Benchmark → goodput 的 harness 改造路径

**核心矛盾**:AIME/GPQA/LiveCodeBench/SWE-bench 的官方 harness 算的是答对率(pass@1 /
exact-match),不发请求、不计时、无并发,测不出 goodput。要在系统层评测,需把「精度
harness」改造成「负载发生器」:把每道题的 prompt 当一条请求,丢弃判分逻辑,接到 serving
harness 上按到达过程并发回放,只采 TTFT/TPOT/完成情况。

**三条具体路径(从轻到重):**

1. **直接喂现成 serving harness(最快)**:`vllm bench serve` 原生支持 `--dataset-name custom`
   (`.jsonl`,每条带 `prompt` 字段)和 `--dataset-name hf`(直接拉 HF 数据集)。把 benchmark
   的 prompt 列导出成 `prompt` 字段即可当负载。配 `--request-rate <R>`(`inf`=压满 / 有限值
   =受控到达)和 `--goodput KEY:VALUE`(直接传 SLO,如 `ttft:2000 tpot:80`,单位 ms),
   harness 原生算 goodput。来源:docs.vllm.ai bench serve CLI。
   - 局限:`custom`/`hf` 默认无真实到达时间戳、无前缀复用结构,适合 AIME/GPQA/LiveCodeBench
     (本就无复用),不适合直接评 SWE-bench 的 KV 复用。

2. **会话重建缝合注入时序/复用(`07` §6.6)**:对要测 KV 复用的负载(SWE-bench agentic / 长文档
   多轮),用 Mooncake trace 提供真实时序/并发/会话结构、真实多轮对话集提供内容,真模型现场生成,
   这样既有真实内容(MTP)又有真实复用结构(KV)。harness 走
   `--dataset-name timed_trace --self-timed --timed-trace-chunk-hash-size 512`(`07` §2)。

3. **跑 agent 框架再镜像流量(最贴真,SWE-bench 专用)**:SWE-bench 的 KV 复用只在 agent 多步
   循环里产生。落地法:用真实 agent scaffold 跑 SWE-bench-Verified,把它对 vLLM 发的每一条
   请求(系统提示+工具定义+累积历史)录成 trace,再用上面的 harness 按真实时序回放,即可
   在系统层量「巨型共享前缀 + 累积历史」的 KV 复用 goodput。

**改造现状(各 harness 对真实 benchmark 负载的支持)**:

| harness | 自定义/真实 benchmark 负载 | 真实时间戳回放 | 前缀复用 hash | 原生 goodput | 备注 |
|---|---|---|---|---|---|
| **vLLM `bench serve`** | `custom`(jsonl)/ `hf` / `sharegpt` / `sonnet` / `random` | `timed_trace --self-timed` | `--timed-trace-chunk-hash-size` | `--goodput KEY:VALUE` | 主力,同源保真 |
| **SGLang `bench_serving`** | 支持 + `generated-shared-prefix`(zipf 前缀热度) | `--use-trace-timestamps` | mooncake | 部分 | 交叉验证 + 建模前缀幂律 |
| **NVIDIA AIPerf**(GenAI-Perf 继任) | `--custom-dataset-type mooncake_trace` | 支持 | 支持 | 原生 goodput | 见 `07`,需校验 hash→token 确定性 |
| **Vidur(模拟器)** | 经长度/到达分布建模(非逐 prompt) | 支持(模拟) | 经复用模型 | 支持 | 无 GPU 时先扫大网格 |

**分配总结**:AIME/GPQA/LiveCodeBench 走路径 1(custom/hf + --goodput),SWE-bench/长文档多轮走
路径 2/3(缝合法 / agent 镜像),全部只保留 prompt、丢弃判分,采 TTFT/TPOT/goodput。

---

## 4. 2026 竞品对标(刷新 `03`/`04` 的 2024–2025 旧版)

> **核对结论(本文最承重的事实):vLLM「成本+SLO 感知前缀驱逐」实际有两条 RFC,不要混淆:**
> - **RFC #37823 = T-LRU(Tail-Optimized LRU)**:OPEN(开放中),2026-03-22 开,反馈期 ≥1 周。
>   机制 = 会话感知两队列,`B = max(0, H + Q̂ − ξ)`(H=已缓存历史块,Q̂=下一轮预估新块,
>   ξ=TTFT 预算/允许的未缓存 prefill 块);blocks 1..B 为「TEL-unsafe」(驱逐会违反 TTFT)优先保留,
>   其余「TEL-safe」先驱。CLI:`--tlru-xi-tokens`(默认 4096)/`--tlru-qhat-tokens`(默认 200)。
>   改动仅限 v1 KV 栈(5 文件 + 新 `tel_safe_queue` + 15 单测,不动 scheduler/kernel/API)。
>   实测 P95 TTFT 较 LRU 降 ≤27.4%,闭合 25–79% 的「LRU→离线最优」差距。
>   状态:仅有参考实现的 feature 分支(`wenxinzhang0/vllm:feature/tlru-eviction-policy`),无已合并 PR。
>   对应论文 = NeurIPS 2025「Tail-Optimized Caching for LLM Inference」(arXiv 2510.15152,`07` 已引)。
> - **RFC #23641 =「Frequency and Cost Aware Eviction」**:已 CLOSED(标 not planned),2025-08-26
>   开。机制 = `Retention Benefit = T·freq·compute_cost`,evict 最低分。注意:`03` 把 T-LRU
>   描述成「重算成本 + TTFT-SLO 感知」更接近 #23641 的成本项;而 #37823 真身是「会话长度感知
>   的尾延迟」,不含显式重算成本/频率项。这条要回写订正 `03`。
> 来源:github.com/vllm-project/vllm/issues/37823 与 /23641。

| 竞品 | 机制 | 价值维度 | 2026 最新状态 / 定量 | 可溯源结论 |
|---|---|---|---|---|
| **vLLM 原生 APC** | 纯 LRU 前缀缓存 | 仅近因 | 稳定;主要击败对象 | `03` §4:不算每字节价值 |
| **T-LRU(RFC #37823)** | 会话感知两队列 `B=max(0,H+Q̂−ξ)` | TTFT-SLO + 会话长度(无成本/频率) | OPEN(2026-03),无合并 PR;P95 TTFT −27.4%,闭合 25–79% gap | NeurIPS'25;仅参考分支,仍是在建竞品,Foretoken 须明确超越 |
| **#23641 成本+频率驱逐** | `T·freq·compute_cost` | 频率+重算成本(无 SLO/每字节) | CLOSED, not planned | 社区未采纳,这条价值维度仍是空位 |
| **SAECache**(arXiv 2605.18825) | 语义感知多队列,token 级保留分(系统提示 vs CoT 权重不同) | 仅 P(reuse)(无成本/SLO) | token-type 权重 +23%、自适应队列权重 +39%;异构负载 TTFT 1.4× | `03` 判断坐实:省略成本+SLO,是 LHD 式每时命中密度的 KV 实例 |
| **LPC**(NeurIPS'25) | 学习型会话续接 P(reuse) | 仅 P(reuse),会话级统一分 | 较 LRU 缓存小 18–47%;被 SAECache 指为「块不分语义」 | `03` 判断成立 |
| **SGLang HiCache** | L1(GPU)/L2(host)/L3(分布式)分层 radix,异步备份+预取 | 复用计数准入 + 近因驱逐 | ≤6× 吞吐、TTFT ≤80% 降;DeepSeek 3FS 实测 TTFT −56%、吞吐 2×、命中率 40%→80% | LMSYS 2025-09;驱逐仍近因,无每字节价值 |
| **LMCache** | 企业级 KV 层(LRU/LFU/FIFO/ARC) | 近因/频率 | 多轮 agentic MI300X blog(2026-05);分层卸载 | `03` §4:无每字节价值 |
| **PiKV**(arXiv 2508.06526) | MoE 专用 KV 管理,expert-aware 驱逐 | query 驱动 + 专家路由感知 | 2–3× 额外吞吐;2026-03 加 FPGA RTL | 2026 新 MoE 调度信号;正交于前缀缓存,但若 Foretoken 触 MoE 路由须引为相关工作 |

**总结**:到 2026,「价值感知 KV 驱逐胜过 LRU」已被 T-LRU/SAECache/HiCache 占满(`03` §0 的
现实核查继续成立,且更紧)。Foretoken 的可守新意仍是 `03` §5 那句窄陈述:把经校准 P(reuse) ×
物理重算成本 × SLO 价值融进单一每字节密度、同治驱逐 AND 跨层准入、并可证退化到
LHD/GDSF/T-LRU,三个对手各缺其中一两维(T-LRU 缺校准复用+每字节;SAECache/LPC 缺成本+SLO;
HiCache 驱逐仍近因)。门槛(`04`)须同时击败 LRU 与 T-LRU/SAECache 这两个真竞品,不止 LRU。

---

## 5. goodput / SLO 的最新定义(2024 → 2026)

- **基线定义(DistServe OSDI'24 / Mooncake)**:goodput = 在 TTFT ≤ a & TPOT ≤ b 且 ≥X% 请求
  达标下的最大 req/s(`04` 已采)。
- **Revisiting SLO/Goodput(arXiv 2410.14257)**:指出裸 goodput 可被作弊(晚放弃、拖延 token
  操纵 TBT);提出用承诺相对截止时间、把重发/放弃计入负载、禁拖延 token(`04` §陷阱 7 已采)。
  并提「smooth goodput」= 单位时间服务收益(收益 = 生成 token 数 + 用户空闲延迟两因素),
  比硬阈值更平滑。
- **2025–2026 新形式化 —— 异构 SLO(承认「一个全局 SLO 不够」)**:
  - **SCORPIO(arXiv 2505.23022)**:把 goodput 推广到异构 SLO 类(不同请求不同延迟约束),
    用准入控制 + 调度最大化「按各自 SLO 达标的完成请求」,较 vLLM/Orca 显著提升。
  - **SOLA(清华 NICS-EFC)**:以 SLO attainment 为直接优化目标的调度。
  - 工业基准的具体 SLO 数值(可直接采用):MLPerf Inference v5.1 server 场景对 DeepSeek-R1
    定 TTFT P99 ≤ 2s、TPOT P99 ≤ 80ms(权威、可引用的业界标准 SLO 旋钮)。
- **对 Foretoken 的含义**:`04` 的单一全局 SLO + ≥90% 达标仍是稳妥头条;但 §6 的
  `SLO_value = w(tenant_priority)·g(deadline_slack)` 正好对应异构-SLO 这条 2026 前沿,评测里
  应至少跑一组「双 SLO 类」(高优/低优租户)来兑现这个价值维度,否则 SLO 项测不出。

来源:DistServe 2401.09670 · Revisiting-SLO 2410.14257 · SCORPIO 2505.23022 · MLPerf v5.1
(mlcommons.org/2025/09/deepseek-inference-5-1)。

---

## 6. 无损校验:严谨验证「输出 == 原生 vLLM」

`07` 每个配置都要求无损校验,但没说怎么做才严谨。核心难点:LLM 推理默认就不确定
(即便 temperature=0),所以「逐字节相同」不能天真比较。分三层做:

**(1) 先消除引擎自身的非确定性 —— batch-invariance**
- 根因(Thinking Machines「Defeating Nondeterminism」2025):非确定性主因不是并发随机,而是
  缺 batch 不变性,RMSNorm/matmul/attention 的归约顺序随 batch 大小变,同一 prompt 在
  不同 batch/并发下结果漂移。实测:默认 vLLM 对 1000 条相同请求(temp=0)产生 80 种不同补全;
  开 batch-invariant kernel 后 1000 条逐字节相同。
- 落地:`export VLLM_BATCH_INVARIANT=1`(vLLM 官方 feature,`docs/features/batch_invariance`)
  使输出与 batch 大小/顺序无关。代价:关掉部分优化、有性能损失;仅 H100/H200/B 系(cc≥9.0)。
- **校验协议**:基线组与处理组都开 `VLLM_BATCH_INVARIANT=1`,固定相同 seed,确认 baseline 自身
  对相同输入逐字节可复现,这是无损校验的前提(否则「不一致」分不清是 bug 还是引擎抖动)。

**(2) KV 复用的无损性 —— 前缀缓存只应改性能、不应改输出**
- 严谨判据:对每条请求,`(APC/价值驱逐 开)` 的输出 token 序列 == `(APC 全关)` 的输出,在
  batch-invariant + 同 seed 下逐 token 比对、≥99.9% 完全一致(KV 命中是数学等价,理论上应
  100%;留极小数值容差给 kernel)。任何系统性偏移 = 缓存重建/块边界 bug(呼应 `04` gate-zero 与
  `07` 块大小对齐 512)。

**(3) 投机/MTP 的无损性 —— 验证「接受后分布 == target」**
- 投机解码号称 lossless 当且仅当输出分布 == 原生 target 模型分布。两种判据:
  - **贪心(temp=0)**:开 MTP 的贪心输出必须逐 token == 关 MTP 的贪心输出(vLLM 的
    *greedy equality test*);任何分歧 = 接受/回退逻辑 bug。
  - **采样(temp>0)**:用拒绝采样校正(Metropolis–Hastings / 概率比),靠分布收敛检验
    (vLLM 的 *rejection sampler convergence test*:大样本下经验分布 == target)而非单条逐字节。
- **实操**:Foretoken 的 MTP 配置至少跑贪心逐 token 等价(最强、最易判),采样场景跑收敛
  检验;两者都报「不一致率」。注意 MTP 必须真模型 + 真内容(`05`/`07`),贪心等价测的是
  *实现正确性*,不是接受率。

来源:Thinking Machines「Defeating Nondeterminism in LLM Inference」(thinkingmachines.ai)·
vLLM batch_invariance / reproducibility 文档 · vLLM speculative_decoding 文档(greedy-equality /
rejection-sampler convergence 测试)。

---

## 7. 对 Foretoken 评测计划的具体修订建议

1. **拆分 benchmark 角色(订正 `07`)**:把「四 benchmark 通吃 KV+MTP」改为明确分工 ——
   KV 复用主线 = SWE-bench-Verified(agent 模式)+ 长文档多轮 QA(有大共享前缀+累积历史);
   MTP 接受率主线 = AIME/GPQA/LiveCodeBench(短入超长出)。理由见 §2。
2. **先实测长度分布再固化**:在 GLM-4.5-Air 上对每个 benchmark 跑一遍,记 input/output 的
   P50/P99(MLPerf 的 800/3,880/20,000 只是 R1 的混合集参考),写入 gate-zero 附录。
3. **harness 路径分配(落 §3)**:AIME/GPQA/LCB 用 `vllm bench serve --dataset-name hf/custom
   --goodput ttft:2000 tpot:80`;SWE-bench 用 agent 镜像 trace + `timed_trace` 回放。
4. **对标加码(订正 `03`/`04`)**:MVP 门槛从「击败 LRU」升为「击败 LRU + T-LRU(用其参考分支)
   + SAECache」;并回写 `03`:T-LRU(#37823)是会话长度/尾延迟感知、不含重算成本项
   (含成本项的 #23641 已 CLOSED)。
5. **加一组异构-SLO 实验**:跑「高优/低优双租户」兑现 `SLO_value` 维度(对齐 SCORPIO 2026 前沿);
   SLO 数值直接用 MLPerf v5.1 的 TTFT P99≤2s / TPOT P99≤80ms 作其一档。
6. **无损校验固化为强制门**:所有配置统一 `VLLM_BATCH_INVARIANT=1` + 同 seed;KV 配置测
   「APC 开 vs 全关」逐 token 等价;MTP 配置测贪心逐 token 等价 +(采样)拒绝采样收敛;报「不
   一致率」。无此门,real 评测站不住。

---

## 8. 开放问题

- **T-LRU 参考实现可用性**:#37823 仅 feature 分支、无合并 PR。Foretoken 要对标须自行拉分支
  跑通;若上游迟迟不合,对标对象是否会变?(待核实:分支是否随 vLLM 主线漂移而失效。)
- **SWE-bench 的 KV 复用强依赖 agent scaffold**:不同 scaffold(SWE-agent / OpenHands / 自研)产生
  的共享前缀与累积历史差异很大,复用画像是否 scaffold-dependent?需固定一个 scaffold 并报告。
- **benchmark prompt 缺真实到达时序**:AIME/GPQA/LCB 本身无时间戳,`--request-rate` 是合成泊松,
  突发性失真。是否需把它们也按 Mooncake 的真实时序回放(而非仅当无时序内容源)?权衡见 `07` §6.6。
- **batch-invariant 的硬件门槛**:`VLLM_BATCH_INVARIANT=1` 仅 H100/H200/B 系且有性能损,在
  A100(GLM-4.5-Air 跑得动)上如何做无损校验?是否退而用「固定 batch/并发 + 同 seed」近似?(待核实)
- **MoE 路由与 KV 价值的耦合**:若后续上 MoE(PiKV 这类 2026 工作),专家路由的 cache 价值与前缀
  KV 价值是否要统一进同一个每字节密度?目前 `03` 的价值函数未覆盖 MoE 维度。

---

## 来源(本文新增;不重复 `04`/`07`/`.research/sources.txt` 已有)
- vLLM RFC #37823 T-LRU(OPEN, 2026-03):https://github.com/vllm-project/vllm/issues/37823
- vLLM RFC #23641 Frequency+Cost Eviction(CLOSED, not planned):https://github.com/vllm-project/vllm/issues/23641
- MLPerf Inference v5.1 DeepSeek-R1(800/3,880/20,000 tok;TTFT P99≤2s / TPOT P99≤80ms):https://mlcommons.org/2025/09/deepseek-inference-5-1/
- LiveCodeBench(511 题统计):https://arxiv.org/html/2403.07974v2
- SWE-bench(轨迹 token 区间):https://www.vals.ai/benchmarks/swebench · https://epoch.ai/benchmarks/swe-bench-verified
- SAECache「Not All Tokens Are Worth Caching」(2605.18825;+23%/+39%/1.4× TTFT):https://arxiv.org/html/2605.18825v1
- SGLang HiCache(≤6× 吞吐 / TTFT ≤80% 降 / 命中率 40→80%):https://www.lmsys.org/blog/2025-09-10-sglang-hicache/
- PiKV(MoE KV 管理,2–3× 吞吐):https://arxiv.org/html/2508.06526
- SCORPIO 异构 SLO goodput(2505.23022):https://arxiv.org/pdf/2505.23022
- vLLM `bench serve` CLI(custom/hf 数据集、`--request-rate`、`--goodput`):https://docs.vllm.ai/en/latest/benchmarking/cli/
- Thinking Machines「Defeating Nondeterminism in LLM Inference」(batch-invariance;80→1 唯一补全):https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/
- vLLM batch invariance 文档(`VLLM_BATCH_INVARIANT=1`):https://docs.vllm.ai/en/latest/features/batch_invariance/
- vLLM speculative decoding 文档(greedy-equality / rejection-sampler convergence 测试):https://docs.vllm.ai/en/latest/features/speculative_decoding/
- Lossless speculative decoding(无损判据综述):https://www.emergentmind.com/topics/lossless-speculative-decoding

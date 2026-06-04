# 13 — 测试与正确性纲领(两层:代码测试纪律 + 评测正确性 checklist)

> **状态:纲领(P0 前)。** 项目尚无代码,本文先将测试拆成两层,确立约定与清单;P0 起代码后,代码测试随
> [`../tests/`](../tests/) 落地,评测 checklist 在每次开跑前强制过一遍。正确性 / 确定性是一等约束
> (`DESIGN §8`、模块 F2),不是事后补的。

## 为什么分两层
"测试"在本项目有两个面,都需严谨:
- **代码正确性测试(L1)** —— 插件(OffloadingManager / CachePolicy / scheduler / connector /
  MTP 控制器)是否改变了模型输出、是否破坏确定性。判据是无损:开优化 == 原生 vLLM。
- **评测正确性(L2)** —— 报出的 goodput / 命中率 / 加速是否真实。判据是可信:回放保真、无陷阱、对标真基线、
  置信区间互不重叠(`docs/04`/`07`/`10`)。

L1 保证"没改坏",L2 保证"测得准"。缺任一,结论都不可信。

## L1 · 代码正确性测试纪律(P0 起随代码落地)

### 一等测试(任何优化"完成"前必过)
| 测试 | 判据 | 怎么测 | 依据 |
|---|---|---|---|
| **无损 · KV** | APC 开 vs 全关,**贪心逐 token 等价** | 同 prompt 集、`temperature=0`、固定种子,逐 token 比对 | `docs/10`;vLLM prefix-caching 测试 |
| **无损 · MTP** | spec 开 vs 关,**贪心逐 token 等价**;采样下**拒绝采样收敛检验** | vLLM 已有 greedy-equality + rejection-sampler 收敛测试,直接借鉴 | S44 vLLM spec-decode docs |
| **确定性** | 相同请求逐字节可复现 | `VLLM_BATCH_INVARIANT=1`(A100 上可行性 + 性能损 **待核实**,见 `docs/10`)+ 固定种子 | S42 / S43 |
| **EPHEMERAL 契约** | draft KV **绝不**被卸载 / 缓存 | 单测:被拒绝 draft 的 `kv_cache_gid` 不进卸载层 / 前缀缓存(`docs/05` 跨模块约束) | `docs/05` |

### 测试分层 + pytest 约定
- `tests/correctness/` —— 无损 + 确定性 + EPHEMERAL 契约(上表),**最高优先级**。
- `tests/unit/` —— 价值函数、`P(reuse)` 估计器、复用 vs 重算成本模型的纯函数单测(可不起 GPU)。
- `tests/integration/` —— 插件在真实 vLLM 上加载、跑通、不崩。
- `tests/eval/` —— L2 的门槛零 / PFOO oracle 作为可执行 gate(见下)。
- **markers**:`slow`(起 GPU / 端到端)、`lossless`、`determinism`、`eval`。**快子集进 pre-commit /
  pre-push;慢的端到端只手动 / CI**(`.pre-commit-config.yaml` 已留 `pre-push` 占位)。

## L2 · 评测正确性 checklist(每次开跑前强制过一遍)
提炼自 `docs/04`(原则)/ `docs/07`(实操)/ `docs/10`(竞品 + benchmark)。**任一不过 = 结论不可报。**

### 开跑前 gate(门槛零先过)
- [ ] **回放保真**:离线 `hash_id` 命中率 ≈ 实测原生 APC 命中率;块大小对齐(512)。
- [ ] **PFOO / Belady oracle 就位**,作为 LRU→最优的上界标尺(报"弥合差距 %"需有分母)。

### 负载与基线(避坑)
- [ ] **不用随机 token**(破坏复用结构);用真实内容(缝合法:Mooncake 骨架 + prompt pool)。
- [ ] **受限容量区间(<2×HBM)**才测得出策略差异(工作集 > 容量)。
- [ ] **真实时间戳 + 重尾流行度 + 联合 SLO 下的 goodput**(非裸吞吐)。
- [ ] **prefill / decode 不混杂**统计;**CUDA-graph vs eager** 不串味(动态 K 待在已捕获桶内)。
- [ ] **benchmark 分工对**:AIME / GPQA / LiveCodeBench 压 MTP;SWE-bench(agent)压 KV(`docs/10`)。

### 报告口径
- [ ] **无损校验通过**(L1)作为硬约束,随每个 goodput 数字一起声明。
- [ ] 对标**真竞争者**(原生 LRU / T-LRU / SAECache / LMCache)+ **PFOO**,不只打 LRU。
- [ ] **≥3 种子 + 95% 置信区间互不重叠**;增益**按复用距离桶**拆,标明集中在哪。
- [ ] 拆出**各优化模块对 goodput 的独立贡献**(4 对照配置:全关 / 只 KV / 只 MTP / 全开)。
- [ ] 统一标尺:SLO 约束下的**每 GPU 字节秒 goodput**;厂商倍数只作上界,不当结论。

## 开放问题
- **A100 上的确定性**:`VLLM_BATCH_INVARIANT` 官方主要面向 H100 / H200 / B 系,A100 上是否可用 +
  性能损 **待核实**(`docs/10 §8`);若不可行,确定性测试的替代判据是什么(固定种子 + 容差?但无损
  要求逐 token,容差与无损相悖)。
- **无损 vs 性能**:batch-invariant kernel 有性能损 → 是否只在"无损校验测试"里开、生产路径关?
- **评测 gate 的自动化**:门槛零 / PFOO 能否做成 `tests/eval/` 里 CI 可跑的断言,而非人工核对。

> 来源见 `.research/sources.txt`(尤其 S33 MLPerf 长度、S42 / S43 batch-invariance、S44 spec 无损
> 测试)与 `docs/04` / `07` / `10`。

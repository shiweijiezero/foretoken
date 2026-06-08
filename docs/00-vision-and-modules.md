# 00 — 愿景与模块地图(总表)

## 愿景

目标是大型 Dense/MoE 模型的工业级部署与推理,覆盖多节点、高并发场景。项目不重造已成熟的各层,而是在 vLLM 之上组装现有模块,仅自建具有差异化价值的部分,并以真实评测检验每一项优化的效果(评测方法见 `docs/04` / `docs/07`)。差异化优化由一组组件构成:价值感知的长生命周期 KV-cache 管理(规模与难度最大的一块)、MTP 投机解码,由 goodput 感知的控制环路串联,统一标尺是每 GPU 字节秒的 goodput。

与市场对齐(`docs/01`):引擎层与控制平面均已成熟拥挤,采用复用;尚未解决的缺口(KV 价值策略、goodput 最优的投机控制)采用自建。本表将 build-vs-use 判定逐模块明确,为每个模块提供定向研究,避免重造已有实现。

## 图例

**Use** = 原样采用现有工具 · **Extend** = 配置/接入现有接口面(不 fork) · **Build** = 差异化代码 · **Fork** = 修改引擎核心(最后手段)。状态:✅ 已调研 · 🔬 需定向研究 · ⬜ 未开始。Phase = P0–P4(`DESIGN.md §6`)。

## A. 引擎 / 执行(单节点计算)— 复用 vLLM

| # | 模块 | 判定 | 工具 / 做法 | 状态 | phase |
|---|---|---|---|---|---|
| A1 | 推理引擎(模型执行、attention、批处理) | **Use** | vLLM v0.22.0(SGLang 可选参考) | ✅ | P0 |
| A2 | 量化(FP8/FP4/INT4/AWQ、KV-quant) | **Use** | vLLM | ✅ | P0 |
| A3 | 并行 TP/PP/EP/DP、attention-DP、wide-EP(MoE) | **Use** | vLLM | ✅ | P0 |
| A4 | CUDA graphs / torch.compile | **Use** | vLLM | ✅ | P0 |
| A5 | 确定性 / 批不变性 | **Use** | vLLM `VLLM_BATCH_INVARIANT` | ✅ | P0/test |

## B. KV-cache 管理 — 差异化核心(`DESIGN.md`、`docs/02/03`)

| # | 模块 | 判定 | 工具 / 做法 | 状态 | phase |
|---|---|---|---|---|---|
| B1 | paged/radix KV + 前缀缓存(APC) | **Use** | vLLM APC | ✅ | P0 |
| B2 | 分层卸载 GPU→CPU→NVMe→远端 | **Extend** | vLLM `OffloadingSpec`/`TieringOffloadingSpec`;挂载 LMCache/Mooncake | ✅ | P1 |
| B3 | 价值感知生命周期:准入/驱逐/TTL(每字节 goodput) | **Build** | 自定义 `OffloadingManager` + `CachePolicy`(规范密度,`docs/03 §6`) | ✅ 设计 | P1(MVP) |
| B4 | 非前缀 / 部分复用(CacheBlend) | **Extend→Build** | 自定义 KV connector,worker 端 `save_kv_layer` blend(LMCache 已验证) | ✅ | P3 |
| B5 | 在线复用 vs 重算成本模型 | **Build** | connector `get_num_new_matched_tokens` + scheduler;PCIe↔HBM 模型 | 🔬 | P2 |
| B6 | KV 传输 fabric | **Use** | NIXL / Mooncake Transfer Engine | ✅ | P2/3 |
| B7 | 跨节点 / 全局 KV(KV 感知路由、共享存储) | **Extend** | connector + 共享内容哈希存储;遵循 Gateway API IGW | 🔬 | P3 |
| B8 | `P(reuse)` 长视野估计器 | **Build** | 按类生存 + 不确定性 + 竞争安全混合 | 🔬 | P1 |

## C. 投机解码 — MTP 关键模块

| # | 模块 | 判定 | 工具 / 做法 | 状态 | phase |
|---|---|---|---|---|---|
| C1 | MTP 投机解码(原生多 token 预测头,如 GLM-4.5 / DeepSeek MTP) | **Use→Extend** | vLLM `glm4_moe_mtp` / deepseek-MTP spec 路径;调 draft 长度,与 scheduler/KV 集成 | 🔬 | P2/3 |
| C2 | goodput 感知投机控制(自适应 draft 长度、SLO 感知) | **Build/research** | SmartSpec/SpecServe/AdaServe 一类,OSS 中未产品化;自定义控制环路 | 🔬 | P3 |
| C3 | 跨词表(cross-vocab)投机解码(此前工作) | **Defer(研究扩展)** | 保留的 cross_vocab;可作为研究扩展恢复 | ✅(已保留) | later |

## D. 调度 / 服务控制(单实例)

| # | 模块 | 判定 | 工具 / 做法 | 状态 | phase |
|---|---|---|---|---|---|
| D1 | 连续批处理 / 分块 prefill | **Use** | vLLM | ✅ | P0 |
| D2 | 调度器(准入、排序、抢占) | **Extend** | 自定义 `scheduler_cls`(价值/goodput 感知) | ✅ 设计 | P2 |
| D3 | P/D 分离 | **Use** | vLLM + Mooncake/NIXL connector | ✅ | P3 |
| D4 | SLO / goodput 感知调度 | **Build/research** | 在 D2 中的 goodput 目标(DistServe/Mooncake 定义) | 🔬 | P2/3 |

## E. 编排 / 控制平面(多实例、集群)— 复用

| # | 模块 | 判定 | 工具 / 做法 | 状态 | phase |
|---|---|---|---|---|---|
| E1 | KV 感知路由器 | **Use/Extend** | Gateway API Inference Extension(遵循);或一层薄封装提供给 B7 | 🔬 | P3+ |
| E2 | 自动扩缩(SLO/KV 利用率感知) | **Use** | Dynamo Planner / llm-d / AIBrix | ✅ | later |
| E3 | 多模型 / 多租户 | **Use/Defer** | AIBrix / KServe | ✅ | later |
| E4 | 部署(K8s、Helm、serving API) | **Use** | vLLM production-stack / KServe / llm-d;OpenAI 兼容的前门 | ✅ | later |

## F. 可观测性 / 正确性

| # | 模块 | 判定 | 工具 / 做法 | 状态 | phase |
|---|---|---|---|---|---|
| F1 | 指标(TTFT/TPOT/goodput/KV 命中/缓存) | **Extend** | vLLM Prometheus + 自定义 KV/goodput 指标 | 🔬 | P0 |
| F2 | 确定性 / 可复现性 | **Use** | vLLM batch-invariant;一等测试 | ✅ | P0 |
| F3 | 追踪 / 调试(按请求的 KV 命中、路由、accept) | **Build** | serving 原生追踪(一个已知的 OSS 缺口) | 🔬 | later |

## G. 基准 / 评测测试台 — 自建(`docs/04` 原则 + `docs/07` 实操)

| # | 模块 | 判定 | 工具 / 做法 | 状态 | phase |
|---|---|---|---|---|---|
| G1 | trace 回放(Mooncake)+ 长生命周期负载 | **Build** | vLLM `bench serve --dataset-name timed_trace` + 自定义长生命周期构造器 | ✅ 设计 | P0 |
| G2 | 基线 + 指标 + PFOO oracle + 门槛零 | **Build** | 原生 LRU / T-LRU / SAECache / LMCache;每字节 goodput;回放正确性门槛 | ✅ 设计 | P0 |

## 自建集合(差异化)vs 复用集合

- **自建:** B3 价值感知生命周期(MVP)· B5 复用 vs 重算 · B8 `P(reuse)` 估计器 · B4 非前缀复用 · D4 goodput 调度 · C2 goodput 感知投机控制 · G1/G2 评测测试台 · F3 追踪。
- **复用/扩展(成熟):** 整个引擎(A*)、KV 机制(B1/B2/B6/B7)、MTP(C1)、批处理/P-D(D1/D3)、整个控制平面 + 部署(E*)、基础指标/确定性(F1/F2)。
- **推迟:** C3 跨词表;E2–E4 集群;F3 追踪,在差异化核心被验证之后。

## 跨模块约束(调研中发现,必须遵守)

- **MTP draft KV 是临时的**(`docs/05`):drafter 的 `kv_cache_gid` 为被拒绝的 draft 写入的 KV 会被丢弃,因此 B2/B3 必须把 draft KV 标记为不可卸载 / 不可缓存(显式标记,而非自动)。这是一个 C1↔B2/B3 契约。
- **动态投机长度(C2)必须待在已捕获的 cudagraph 桶内**,否则 eager 回退会抵消增益。
- **MoE KV 按 DP rank 分区**(`docs/02`),因此任何跨 rank/全局策略(B7)= 一个 connector + 共享内容哈希存储,而非单一块管理器。

## 定向研究队列(每个 🔬 模块,在其 phase 之前)

- ✅ **B8 `P(reuse)` 估计器** → `docs/06`(因子化的 流行度×生存;仅 token 类型的 MVP;竞争安全混合)。
- ✅ **C1 MTP** → `docs/05`(C1 = 复用 vLLM 原生 MTP;C2 = 自建 goodput 感知的 MTP 控制器)。
- ✅ **D4/C2 goodput 控制** → `docs/11`(统一 goodput 控制环路;KV 命中↔投机长度耦合 = 文献空白;跨层反馈设计草图 + 开放问题)。
- 🟡 **B5 复用 vs 重算** — `docs/09` 给出 PCIe↔HBM 成本量级 + A100 重算实践;在线成本模型本体待 P2。
- 🟡 **B7/E1 KV 感知路由** — `docs/09`(per-DP-rank KV 无全局视图)+ `docs/12`(多维对标 / 跨节点到达);路由设计本体待 P3。
- 🟡 **F1 指标** — `docs/10`(`vllm bench serve --goodput` 采集 + benchmark→goodput 改造);完整遥测面待 P0/P2。

## 2026-06 新增:三支柱定位扩展的定向调研(工业级推理 + 优化 + 真实评测)

- ✅ **工业级推理主体** → `docs/09`(大型 MoE 多节点部署、量化 A100 无原生 FP8 硬约束、长上下文成本、高并发/SLO)。
- ✅ **评测实践落地** → `docs/10`(benchmark↔trace 对齐、benchmark→goodput 改造、2026 竞品定量对标、无损校验)。
- ✅ **真实负载画像** → `docs/12`(reasoning/agentic/多轮/RAG/长上下文 实测长度 + 复用结构 + 到达模式 + 多维对标)。
- ✅ **测试与正确性纲领** → `docs/13`(L1 代码无损/确定性 + L2 评测正确性 checklist)。

每个 🔬 行在其 phase 启动前都有一份定向研究笔记(一次 `docs/` 深挖)+ 一个 build-vs-use 确认。研究队列已大幅覆盖(B5/B7/F1 部分覆盖,余下随各自 phase 深化);P0(真实推理基线)不被任何剩余研究项阻塞,可起步(见 `ROADMAP.md`)。

## 代码骨架命名约定(`src/foretoken/`,2026-06)

- **包 = `foretoken`**(命名空间)。不预先建立空占位:某模块 P1/P2 真正实现时才建立,命名照此约定。
- **`data_prepare/`**(P1)— 评测负载的数据准备:由 Mooncake 重建会话与时序结构、填入真实多轮对话(`make_workload.py`),打包为可复现 HF 数据集(`build_dataset.py`,入口 `scripts/build_dataset.sh`)。data_prepare 生成负载,`bench/` 回放负载。
- **`bench/`** — 评测台:`replay.py` 进程内自起 vLLM 引擎(`AsyncLLM`)、按真实 timestamp 闭环回放 `data_prepare/` 的负载(现场生成回复拼接下一轮),采 TTFT/TPOT、算原始吞吐与 goodput;`report.py` 出可复现记录(`summary.md` / CDF·直方图·对照图 / `INDEX`);入口 `scripts/bench.sh`(配置加载见顶层 `foretoken.config`)。自建闭环(非 `vllm bench serve`)因后者无法同载真实 prompt + 真实到达时刻、且评测需闭环。后续补门槛零(回放保真)/ PFOO·Belady 最优上界 / 配置拆贡献。
- **`plugins/`**(P1 起)— 优化插件 wrapper,子模块对齐 vLLM 命名以降低认知负担:
  - `kv_offload/`(B3,对 vLLM `OffloadingManager`/`Spec`,`v1/kv_offload/`)
  - `cache_policy/`(对 vLLM `CachePolicy`,`v1/kv_offload/cpu/policies/`;含 value / `P(reuse)`)
  - `sched/`(D2/D4,对 `Scheduler`,`v1/core/sched/`)
  - `spec_decode/`(C2,对 `custom_class` proposer,`v1/spec_decode/`)
  - `kv_connector/`(B4,对 `distributed/kv_transfer/kv_connector/`)
- 本地参考:vLLM 源码经 junction 挂在 `vendor/vllm/`(已 gitignore,只读参考)。

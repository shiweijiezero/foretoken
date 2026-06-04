# Foretoken

*foretoken — 预兆;字面 fore + token:预判该留的 KV、预测下一批 token,把真实推理跑得更快。*

一个基于 vLLM 的开源项目,目标分为三部分:① 把工业级真实推理跑好、能服务(主体);② 在其上做
差异化优化——KV 管理、MTP 投机解码、goodput 调度,其中 KV 是最大、最难的一块(手段);③ 用
真实评测检验每一项优化的效果(裁判)。主体是真实推理,优化是手段,评测是裁判,三者并列。
项目不构建新的引擎或控制平面,而是全程通过 vLLM 的官方扩展点集成、不 fork(已对照最新源码
核实,见 [`docs/08`](docs/08-vllm-extension-points.md))。当前从单人维护起步,先聚焦 LM 长上下文 /
reasoning,再逐步扩展。路线见 [`ROADMAP.md`](ROADMAP.md);模块的自建 vs 复用判定见
[`docs/00`](docs/00-vision-and-modules.md)。

> 状态:设计 / 文档梳理完成,即将进入 P0(真实推理基线)。定位、评测方案(一套真实场景 + 4
> 对照配置)、不 fork 集成路径、vLLM 扩展点设计均已沉淀到 `docs/` 与 `ROADMAP.md`;环境(最新 vLLM
> 已 clone、A100 集群就绪)齐备。P0 不依赖任何升级,空卡 + 用户态即可起步。

## 论点(Thesis)
主体是工业级真实推理:复用 vLLM 把大型 Dense/MoE + 真实业务负载在多节点、高并发下
跑好、能服务——这是地基,也是第一个交付目标(P0)。优化是地基之上的差异化手段:
LLM 推理的引擎层(vLLM/SGLang/TRT-LLM)和控制平面层(Dynamo/llm-d/AIBrix/KServe)
都已收敛且拥挤,再做其中任何一个都收益有限;KV-cache 的机制(paged/radix cache、分层
卸载、KV 感知路由、P/D 分离、传输 fabric)也已成熟。尚未被充分攻克的是这些机制之上的
策略层 / 优化大脑:现有系统多用各自为政的、启发式的、基于近因(recency)的策略
(LRU/LFU/S3FIFO)治理 KV,投机长度也多为固定值。因此优化集中在价值 / goodput 策略——把
KV 当作一等的、按价值定价的资源,用单一标尺治理:每 GPU 字节秒的 goodput
(goodput-per-GPU-byte-second),并以同一标尺驱动 MTP 投机控制与调度,服务 agentic/RAG/多轮
所需的长生命周期(数小时 / 数天)复用。KV 是其中最大、最难的一块,因此优先攻克。判断每项
优化是否更好,依赖真实评测(见下)。

## 为什么是 vLLM、为什么是 KV
vLLM 是值得押注的引擎(PyTorch Foundation、事实上的底座、最佳插件面)。它的 KV 复用正是
提升空间所在:块级 APC、仅前缀、纯 LRU、没有价值 / SLO 感知的生命周期,没有小时 / 天级的
持久化模型——落后于 SGLang 的 token 级 radix。把 vLLM 的 KV 做到业界最佳,既高影响、
又有明确空间、且恰好命中长生命周期复用这个目标。

## 集成思路(树外插件;MVP 不 fork 核心)
- 自定义 `OffloadingManager`/`OffloadingSpec`：价值感知的长生命周期(按
  `P(reuse)×recompute_cost×SLO_value` 做准入 / 驱逐 / TTL)。长生命周期状态存活在卸载层
  (GPU 保持 LRU,GPU 块在压力下无论如何都会被回收)。
- 自定义 `Scheduler`(`scheduler_cls`)：价值感知的准入 / 排序 + 复用 vs 重算。
- 自定义 KV connector：成本模型;后续支持非前缀 CacheBlend、跨 rank 协同。
- 两处 core fork(GPU 层驱逐、token 级缓存)推迟,直到插件被证明不够用为止。

## 路线与门槛(详见 [`ROADMAP.md`](ROADMAP.md))
- P0 真实推理基线:GLM-4.5-Air + 现成 benchmark 跑通,测 TTFT/TPOT/goodput(不依赖升级、空卡即可)。
- P1 KV 管理 MVP:价值感知卸载策略(纯插件)在真实场景负载上、于受限容量区间(<2×HBM)的
  每 GPU 字节 goodput 上击败原生 vLLM LRU,无损校验通过——第一个可证伪门槛;未达成则 KV 论点不成立。
- P2 MTP:用现成内嵌 MTP + 自适应控制;新 MTP 算法走 `custom_class`(不 fork)。
- 评测贯穿:一套真实场景负载 + 4 对照配置(全关 / 开 KV / 开 MTP / 全开),拆各模块贡献 + 无损校验。

## 评测:唯一的裁判(`docs/04`/`docs/07`)
评测是与推理、优化并列的第三个核心支柱,而非事后配套:厂商倍数是营销上界,启发式策略均自称
最优,需以真实评测验证。评测采用真模型现场生成、真实业务负载、Mooncake 真实时序,对标
真正的竞争者(原生 LRU / T-LRU / SAECache / LMCache)与 PFOO 离线最优,做无损校验
(输出 == 原生),≥3 种子且置信区间互不重叠。每个优化模块在“完成”前都必须
交付一个对标基线的可证伪门槛(未达成即判定失败),并能从一套负载里拆出各模块对 goodput
的独立贡献。统一标尺:SLO 约束下的每 GPU 字节秒 goodput。

## 非目标
不做从零开始的引擎。不做跨引擎层(单引擎 vLLM)。不做通用控制平面。不做新的传输 fabric/
卸载层(挂载 NIXL/Mooncake/LMCache)。确定性复用现成的(`VLLM_BATCH_INVARIANT`),不重造。
不做版本命名。

## 文档
- [`docs/DESIGN.md`](docs/DESIGN.md) —— 架构(基于 vLLM v0.22.0)+ 路线图(P0–P4)。
- [`docs/01-market-landscape.md`](docs/01-market-landscape.md) —— 三条战线的市场调研综述。
- [`docs/02-vllm-kv-hookpoints.md`](docs/02-vllm-kv-hookpoints.md) —— vLLM KV 钩子点地图(file:line)。
- [`docs/03-value-function.md`](docs/03-value-function.md) —— 价值感知驱逐理论、
  **拥挤前沿的现实核查**(SAECache/LPC/vLLM-T-LRU),以及狭窄但可守的新意。
- [`docs/04-eval-methodology.md`](docs/04-eval-methodology.md) —— 负载/trace、指标、基线、
  陷阱,以及可证伪的 MVP 门槛。
- [`docs/05-mtp-spec.md`](docs/05-mtp-spec.md) —— MTP 投机解码:C1 复用 vLLM 原生 MTP,
  C2 自建 goodput 感知的自适应控制。
- [`docs/06-preuse-estimator.md`](docs/06-preuse-estimator.md) —— `P(reuse)` 长视野估计器
  (模块 B8,最难的核心):因子化的 流行度×生存 + 竞争安全混合。
- [`docs/07-eval-playbook.md`](docs/07-eval-playbook.md) —— 评测实操手册(贴近真实业务):
  用哪个 trace、敲什么命令、报哪几张图、对标谁、开跑前 checklist。
- [`docs/08-vllm-extension-points.md`](docs/08-vllm-extension-points.md) —— vLLM 扩展点设计:
  全部留口子的机制 + 开放/封闭取舍 + 设计原则(guest 用法 + host 借鉴)。
- [`docs/09-industrial-inference.md`](docs/09-industrial-inference.md) —— 工业级推理主体:大型
  MoE 多节点部署、量化(**A100 无原生 FP8** 等硬约束)、长上下文成本、高并发/SLO。
- [`docs/10-eval-in-practice.md`](docs/10-eval-in-practice.md) —— 评测实践落地:benchmark↔trace
  对齐、benchmark→goodput 改造、2026 竞品定量对标、无损校验做法。
- [`docs/11-goodput-control-loop.md`](docs/11-goodput-control-loop.md) —— 统一 goodput 控制环路:
  把 KV+MTP+调度串成一个反馈大脑(**KV 命中↔投机长度耦合 = 可守空位**)。
- [`docs/12-workload-profiles.md`](docs/12-workload-profiles.md) —— 真实负载画像:reasoning/
  agentic/多轮/RAG/长上下文的实测长度与复用结构、到达模式、多维对标。
- [`docs/13-testing-and-correctness.md`](docs/13-testing-and-correctness.md) —— 测试与正确性纲领:
  L1 代码无损/确定性测试 + L2 评测正确性 checklist。

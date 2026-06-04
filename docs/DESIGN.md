# Foretoken —— 基于 vLLM 的工业级推理与优化(KV 管理为最大模块)

> 本文是详细设计参考,偏重优化模块(尤其 KV)的深度设计。顶层定位以
> [`ROADMAP.md`](../ROADMAP.md) 为准:主体是工业级真实推理(真实模型 + 真实业务负载、
> 能服务),KV 管理、MTP 投机解码、goodput 调度是其上的差异化优化模块,真实评测是
> 检验每一项优化好坏的裁判。MTP 已纳入路线(`docs/05`);cross_vocab 此前工作保留可恢复(C3)。

项目采用单一引擎 vLLM,把工业级真实推理跑好,并在其上把 KV-cache 管理这块最大、最难
的优化做到极致,面向大型 Dense/MoE、多节点、高并发,以及长生命周期 KV 复用。以
树外(out-of-tree)vLLM 插件形式构建(MVP 不 fork 核心),并预留两处隔离良好的 core
fork 留待后续。设计立足于一份三战线市场调研 + 一份 vLLM v0.22.0 的 file:line 钩子点地图 +
一份价值函数 / 评测方法论的深挖(`docs/01`–`04`)。

> 状态:研究 / 设计阶段,尚无代码。先对照 vLLM 真实内部实现敲定定位 + 架构,再动手
> 实现,以避免返工和版本动荡。

## 1. 为什么是 vLLM、为什么是 KV
vLLM 是值得押注的引擎(PyTorch Foundation、Apache-2.0、事实上的底座、最佳插件面、团队
稳定)。它的 KV 复用正是提升空间:块级 APC、仅前缀、纯 LRU,没有价值 / SLO 感知的生命
周期,没有小时 / 天级持久化——落后于 SGLang 的 token 级 radix。把 vLLM 的 KV 做到业界最佳,
既高影响、又有明确空间、且恰好命中长生命周期复用目标。

## 2. vLLM 内部待填补的缺口(按重要性排序)
1. 价值感知的长生命周期缓存生命周期 *(MVP)* —— 以最大化每 GPU 字节秒的 goodput
   为目标的准入 / 驱逐 / TTL,横跨 GPU→CPU→NVMe→远端各层,面向小时 / 天级持久化。
2. 非前缀 / 部分复用(CacheBlend 式)—— 不构成共享前缀的 RAG 分块。
3. 在线的复用 vs 重算成本模型 —— 按块 / 层,对标 ~50–100× 的 PCIe↔HBM 落差。
4. 比块更细的复用 —— 朝 token 级方向(vLLM 浪费了部分块匹配)。
5. 大型 MoE 多节点 KV —— attention-DP + wide-EP 下的价值感知 KV;长上下文之墙。

## 3. 架构 —— 基于 vLLM v0.22.0(`docs/02`)
MVP 和大部分愿景都是树外插件;只有两件事需要 core fork(已推迟)。
- 接入面(不 fork):自定义 `OffloadingManager`/`OffloadingSpec`
  (`spec_module_path`)= 价值感知的长生命周期(准入 `prepare_store`、经自定义 `CachePolicy`
  驱逐、分层);自定义 `Scheduler`(`scheduler_cls`)= 价值感知的准入 / 排序 + 复用 vs 重算
  闸门;自定义 KV connector(`kv_connector_module_path`;`bind_gpu_block_pool` → GPU 池
  句柄;worker 端 `save_kv_layer` → 写入任意 KV slot)= 成本模型 + 后续的非前缀 CacheBlend
  + 跨 rank。
- 关键设计:小时 / 天级持久化不是一个 GPU 概念(GPU 块在压力下无论如何都会被回收),因此长
  生命周期状态存活在卸载层;GPU 保持 LRU。
- 推迟的 core fork:(1)价值感知的 GPU 层驱逐(受害者块被硬编码到近因 free 队列,
  没有接缝);(2)token 级前缀缓存(块量化)。仅当卸载层的天花板成为瓶颈时才做。
- 硬约束:scheduler↔connector 之间的外部命中通道是一个标量前缀计数,因此非前缀复用必须
  走 worker 端 blend(PIECEWISE cudagraph;LMCache 已验证),所以生命周期(MVP)是正确的第一
  目标,非前缀留待后续。
- 已具备的能力:确定性已交付(`VLLM_BATCH_INVARIANT`),与 SGLang 持平。MoE wide-EP:
  KV 按 DP rank 分区,跨 rank 缓存 = connector + 共享内容哈希存储。

## 4. 新意与竞争安全(由 `docs/03` 锐化)
该领域已较为拥挤(2025 末 / 2026)。“价值感知 KV 驱逐胜过 LRU”并非新方向:SAECache
(arXiv 2605.18825)是针对 KV 块的 LHD 命中密度策略;LPC(NeurIPS'25)学习复用;vLLM 的
T-LRU RFC #37823(仍开放、仅参考分支、未合并)在构建尾延迟 / 会话长度感知的 TTFT-SLO
前缀驱逐(`B=max(0,H+Q̂−ξ)`),本身不含重算成本项(重算成本是另一个已关闭的 RFC #23641)。
它结合了 SLO + 粗糙复用,但按会话(per-conversation)而非每字节密度;Q̂ 是启发式的,不是经
校准的 P(reuse)。TRT-LLM 已交付优先级 + retention 的 KV 驱逐(SLO 作为一个手动旋钮)。
可守的新意是:一个单一的每字节期望 goodput 密度,把经校准的 P(reuse)
(持久化感知)× 物理上有依据的重算 vs 重载成本 × SLO / 租户价值融合进单一排序,同时治理
驱逐与跨层准入,并可证明地退化到 LHD、GDSF、T-LRU 作为特例。已有前作均未融合
这三者:SAECache/LPC 省略了成本 + SLO(且在单轮上回归);T-LRU/TRT-LLM 省略了经校准的复用
+ 每字节密度;OrbitFlow 是按请求而非每字节;EVICPRESS 的效用是为压缩服务的。最困难、最有
贡献的部分是长视野(小时 / 天)的 `P(reuse)` 估计器(SAECache 在单轮上做回归;不存在经
校准的非平稳长尾模型)。竞争安全从第一天内建:把学习到的密度与一个鲁棒策略
(S3FIFO/LRU)混合,使最坏情况保持有界,不盲信估计器。
规范目标:`Value(b) = P(reuse|Δt,class)·recompute_cost·SLO_value / (size·E[residency|class])`。

## 5. MVP 与可证伪门槛(`docs/04`)
一个自定义的价值感知 `OffloadingManager`(规范目标;热路径解析式 + LHD 64 采样排序 +
后台按类参数学习;GPU→CPU→NVMe 各层),是一个纯树外插件。门槛:在长生命周期 Mooncake
回放上(实时时间戳、工作集 > GPU 容量、30–50% 的结构性命中天花板),在等量 cache-byte 预算
下以 ≥15% goodput 击败原生 vLLM LRU APC,增益集中在长复用距离桶(≥1 分钟),≥3 个
种子且 95% 置信区间互不重叠,弥合 ≥40% 的 LRU→PFOO 差距,无 TPOT 回退,并逼近 / 击败
真正的竞争者(T-LRU、SAECache),而不只是 LRU。确定性保持不变。

## 6. 路线图(每个阶段都有一个对标基线的可证伪门槛)
- P0 —— 脚手架 + 基线测试台。仓库 + vLLM 插件入口点 + Mooncake 回放测试台;门槛零:
  实测原生 APC 命中率 ≈ 离线 hash_id 命中率(回放正确性)+ PFOO oracle。
- P1 —— 价值感知 OffloadingManager(MVP)。门槛见 §5。
- P2 —— 复用 vs 重算成本模型 + 价值感知 Scheduler(并发 / KV 压力下的 goodput)。
- P3 —— 非前缀 CacheBlend connector + 跨 rank 共享哈希存储(超越前缀的 RAG;多实例)。
- P4(可选)—— GPU 层价值感知驱逐的 core fork,仅当卸载层天花板成为瓶颈时。
- 后续 —— 投机解码,作为这个环路里又一个 goodput 杠杆。

## 7. 明确的非目标
不做从零开始的引擎。不做跨引擎层(单引擎 vLLM)。不做通用控制平面。不做新的传输 fabric /
卸载层(挂载 NIXL/Mooncake/LMCache)。确定性复用而非重造。不做版本命名。

## 8. 严谨性承诺
每个决策都引用 vLLM v0.22.0(file:line)+ 一个一手来源,并对标原生 vLLM + 真正的竞争者
(T-LRU/SAECache/LMCache)+ PFOO 离线最优做基准测试。每个组件在“完成”前都交付一个可证伪
指标。确定性 / 正确性作为一等的测试。插件优先;任何 core fork 都隔离开,并由实测的天花板来
证明其必要。厂商倍数为上界,以自有实测为准。

## 9. 开放设计问题(P1 之前的深挖)
- `P(reuse)` 估计器 + 按类的生存拟合 + 不确定性(最难的部分);SLO_value 模型。
- 面向小时 / 天级持久化的驱逐 / TTL 数据结构 + 开销预算;64 采样排序的实现。
- 把价值信号穿过 `ReqContext`(当前只有 `req_id`+`kv_transfer_params`)。
- 基准测试台的具体细节(Mooncake 回放的 hash→token 确定性,即门槛零);确切的基线。
- 上游贡献 vs 维护 fork 的取舍(插件更利于上游化)。

## 10. 状态
引擎已锁定为 vLLM(2026-06-01)。MVP = 价值感知 OffloadingManager。vLLM v0.22.0 + 环境就绪。
调研已从 KV 中心扩展到三支柱(工业级推理 + 优化 + 真实评测):钩子点地图、价值函数、评测
方法与实践、MTP、`P(reuse)` 估计器、工业级推理主体、goodput 控制环路、负载画像、测试纲领均已
沉淀(`docs/01`–`13`)。仓库:github.com/shiweijiezero/foretoken(私有)。下一步以 `ROADMAP.md`
为准:P0 真实推理基线不被任何剩余研究项阻塞、可起步;B5/B7/F1 等随各自 phase 深化。

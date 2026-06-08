# 15 — Token 工厂技术路线(栈分层 · 买/造 · 阶段)

> **终极目标**:为沐曦(MetaX)GPU 集群提供工业级推理服务(token 工厂)。
> 近期在 A100(80G)/ 4090(24G)上开发与评测;沐曦 MACA 主打 CUDA 兼容,**代码与方法学可迁移,
> 数字须换硬件重测**——这正是评测台(QC)的价值。原则贯穿全篇:**不重建引擎、不自建编排,
> 只造「工艺 IP(策略)+ QC(评测台)」,全程零 fork 走 vLLM 官方扩展点**(见 `docs/08`)。

## 定位:三层栈

工厂类比:生产线 = 引擎,物流调度 = 编排,QC + 工艺优化 = 本项目。

| 层 | 职责 | 代表 | 买 / 造 |
|---|---|---|---|
| **引擎** | 单机/单节点产 token(paged KV、连续批、量化、spec decode、KV connector) | vLLM | **用** |
| **编排** | 集群级:KV-aware 路由、disaggregated P/D、按 SLA 自动扩缩、多模型、K8s | Dynamo / llm-d | **规模到了再用** |
| **策略 + 评测** | 价值感知 KV / MTP / 调度策略 + 真实负载 goodput 评测 | **foretoken** | **造(护城河)** |

不做:另一个引擎(vLLM 已极致)、另一个编排器(NVIDIA/RedHat 重金在投,自建追不上)。

## 阶段:vLLM 即可 vs 上编排

### 阶段一(现在,卡少,A100 / 4090)
vLLM 足够:`vllm serve` + DP + prefix-cache + KV/MTP 插件,单/少实例;disaggregated 用 **vLLM 原生 1P1D**
(`examples/disaggregated`)先试。**不上 Dynamo**。评测三后端(进程内 / `--endpoint` / `--serve`)已支持,
`--engine-param` / `--serve-arg` 透传任意引擎旋钮做 A/B(见 `docs/07`)。

### 阶段二(上规模:多节点 / 多模型 / disaggregated 自动扩缩)
加编排层,**用 Dynamo 或 llm-d,不自建**。Dynamo backend 无关(吃 vLLM 的 KV connector),沐曦上亦可用
(NIXL 那块换 MACA 等价物)。Dynamo 提供:KV-aware 路由、disagg P/D 编排、SLA-driven planner(按延迟
SLA 自动扩缩 P:D,最小化 TCO)、多层 KVBM、K8s。

## KV offload(P1 主战场):机制 vs 策略

| | 是什么 | 买 / 造 |
|---|---|---|
| **机制**(KV 去哪 / 怎么搬) | vLLM `v1/kv_offload/`(CPU 层 + worker)、`OffloadingConnector`、LMCache(多层 GPU/CPU/SSD)、Mooncake;集群级 Dynamo **KVBM**(多层 + 全局 KV events + NIXL) | **用** |
| **策略**(搬什么 / 驱逐什么 / 何时提升) | vLLM 默认 **LRU**(`block_pool` free-queue) | **造(value-aware)** ← P1 |

零-fork 挂载点(源码确认):
- `vllm/v1/kv_offload/cpu/policies/base.py` = 驱逐/准入**策略 ABC** → 自定义 **value-aware policy**
  (token-type 分层:系统提示高 / 会话历史中 / 一次性内容低 + S3FIFO,零模型推理)挂这(见 `docs/06`)。
- `OffloadingConnector` + factory / `OffloadingSpec` 接进引擎(经 `kv_transfer_config` / `scheduler_cls`)。
- **为什么 offload 层而非 GPU 层驱逐**:GPU 层驱逐无官方接缝(要 fork),offload 层有 ABC → 零 fork。

> **⚠️ 架构约束(2026-06-09 实测,见 `docs/16`)**:KV offload 与 disaggregated P/D 都依赖**外部 KV
> connector**,而 vLLM 0.22.1 的 **Mamba 块调度器显式拒绝外部 KV connector**(`scheduler.py`
> `_mamba_block_aligned_split: External KV connector is not verified yet`)。故 **offload / disagg 这两条
> KV 编排路线目前只适用于标准注意力模型**;对混合 / Mamba 模型(如 Qwen3.6-27B),要么等上游支持、要么换
> 机制(LMCache)、要么仅在满注意力层做。**选型含义**:若把 KV offload / disaggregated 作为核心杠杆,
> 模型侧应优先标准注意力架构,或将混合模型的 KV 编排显式列为需上游协作的风险项。价值感知 KV 策略(P1)的
> 挂载点同样受此限制。

**卡少 → offload 价值更大**:让一张卡装下更多「有效」KV(更高复用)= 每卡更多 goodput,正是 token 工厂
卡少时的核心杠杆。对标:vLLM LRU / T-LRU / LMCache / SGLang HiCache / Mooncake + Belady 离线上界(`docs/03`)。

## 可拓展 / 差异化(都走扩展点,零 fork)

1. **价值感知 KV 管理**(头牌,P1)——value-aware 驱逐/卸载/复用 vs LRU。引擎与编排器只给机制,策略开放。
2. **自适应 MTP / 投机控制**(P2)——按负载动态调 spec 长度(goodput 感知;`docs/05`)。
3. **goodput 感知调度 / 准入**——DistServe / Sarathi 策略层(`docs/11`)。
4. **价值导向的 KV-aware 路由**——按「价值」而非仅「presence」路由(对照 Dynamo / SGLang cache-aware 路由)。
5. **真实负载 goodput 评测台**(QC,已建)——厂商倍数是营销、启发式都自称最优,只有真实评测说了算。

## 近期实验清单(A100 / 4090)

- vLLM 原生 **1P1D disaggregated** × 我们的 trace → 量对 TTFT / goodput 的影响。
- **CPU offload on/off × 受限容量(<2×HBM)** → goodput / 每字节 goodput。
- **value-aware policy vs LRU**:命中率 + 每字节 goodput(门槛 = 受限区间胜 LRU,否则判失败;`docs/04`)。
- **MTP `num_speculative_tokens` 扫描 × 负载** → 找「低负载赢 / 高并发反伤」拐点(hybrid 模型需同时关
  chunked prefill,见 `docs/05`)。
- **reuse-distance 分桶命中率**(<10s / <10min / >10min)—— value 函数的依据,差异化指标。
- **4090 FP8 vs A100 bf16** 的 per-card goodput 经济性(A100 无硬件 FP8,4090 有原生 FP8)。
- **无损校验**:开优化输出 == 原生 vLLM(字节级)。

## 硬件可移植(为上沐曦)

- **核心保持硬件中性**:不硬编 NVIDIA 专有(NIXL、cu13 特定、Dynamo 的 NV 件);走 vLLM `Platform` 抽象 +
  官方扩展点,底座从 CUDA 换 MACA 时本层基本不动。
- **MACA CUDA 兼容** → 代码 / 插件 / 方法学迁移成本低;但 **goodput 数字、哪档 FP8 / 量化真有硬件加速、
  最优 tp/dp、prefix-cache/MTP 实际收益,必须在沐曦重测**——评测台换机重跑 `compare` 即得。
- **待真上沐曦确认**:MACA 版 vLLM 支持的 kernel / 特性(flashinfer JIT、哪档 FP8、特定 attention kernel)。

## 一句话

生产线买现成(vLLM),物流到规模再上现成(Dynamo / llm-d),自己只造「工艺 IP(价值感知 KV / MTP / 调度)
+ QC 评测台」,全程零 fork、硬件中性 —— 这是卡少、单人、终要上沐曦时最优的投入分配。

# 01 —— 市场全景(2026 年中)

一份三战线调研的综述:推理**引擎**层、**编排/控制平面**层,以及**分布式长生命周期
KV-cache**系统。结论:不要做引擎、不要做控制平面、也不要做传输 fabric;在单一引擎(vLLM)
内部构建 **KV 策略/优化大脑**。所有论断均有据可查;厂商吞吐倍数为上界。

## A. 引擎层 —— 已收敛、商品化 → 在其之上构建,别重造
| 引擎 | 角色 | KV 复用 | 可扩展性 | 备注 |
|---|---|---|---|---|
| **vLLM** | 事实上的底座(PyTorch Foundation、Apache-2.0) | **块级 APC、仅前缀、LRU** | 最佳(插件、KV-connector、`LLMEngine`) | 值得在其上构建的那个 |
| **SGLang** | 并列标准 | **token 级 RadixAttention + HiCache**(领先) | fork 才能扩展;商业化(RadixArk) | KV 复用前沿 + 确定性 |
| **TRT-LLM** | NVIDIA 性能天花板 | paged + 复用 | PyTorch 后端默认(v1.0) | 厂商锁定 |
| **TGI** | — | — | — | **2026-03 已归档**(许可证的前车之鉴) |
| gpt-fast / llama.cpp | 参考 / 边缘 | — | — | 借鉴其模式,别在其上构建 |

已收敛的"标准栈":paged KV + 前缀/radix 缓存 · 连续批处理 · 统一的分块 prefill 调度器 ·
隔离 CPU 开销的多进程循环 · torch.compile + CUDA graphs · P/D 分离 + 面向 MoE 的 wide-EP。
**vLLM = 别人在其上构建的那个库**(llm-d、KServe、Ray Serve 都封装它)。→ 重造引擎是负 EV。

## B. 控制平面 / 编排层 —— 拥挤的圈地运动 → 别做第 5 个
| 系统 | 所有者 | 已收敛的功能集 | 许可证 |
|---|---|---|---|
| **Dynamo** | NVIDIA | KV 感知路由器 + Planner 自动扩缩 + KVBM 分层 + NIXL | Apache-2.0 |
| **llm-d** | Red Hat+Google+IBM+CoreWeave | IGW 路由 + P/D + KV 感知(CNCF Sandbox) | Apache-2.0 |
| **AIBrix** | ByteDance | LLM 感知网关 + KV 利用率自动扩缩 + 分布式 KV 池 | Apache-2.0 |
| **KServe** | CNCF | K8s 底座;把 LLM 路由委托给 llm-d | Apache-2.0 |

全部 Apache-2.0、多引擎,且都收敛到(KV 感知路由器 + P/D 分离 + SLO 自动扩缩)。智能路由的
**接口**正在标准化(Kubernetes **Gateway API Inference Extension** / Endpoint Picker)。→ 遵循
标准;不要做第 5 个控制平面。

## C. KV 机制 —— 成熟且已收敛 → 挂载,别重造
- 单节点:PagedAttention(vLLM)、RadixAttention(SGLang)。3–4 层卸载 GPU→DRAM→NVMe→
  远端:**LMCache、SGLang HiCache、Dynamo KVBM、AIBrix**。**PCIe 之墙**:GPU↔CPU ~32–63 GB/s
  vs HBM ~3–8 TB/s = ~50–100× 的落差,主宰着每一个复用 vs 重算决策。
- 分布式:经由 KV-events 的 KV 感知路由(Dynamo / llm-d 近似+精确 / AIBrix);传输 fabric
  **NIXL**(NVIDIA,新兴标准)+ **Mooncake Transfer Engine**(久经考验,Kimi)。P/D 分离自
  2025 年起成为标准(DistServe goodput、Mooncake FAST'25)。
- MoE:**attention-DP + wide-EP** —— KV 只来自 attention(专家不贡献 KV);KV 按 DP rank
  分区。MLA 把 KV 压缩约 93%(DeepSeek)。长上下文 KV 是内存之墙(70B @ 1M tok →
  ~135 GB KV > 权重)。

## D. 真正的缺口(尚未攻下)—— 我们的战场
KV 的*机制*已被解决;治理它们的*策略*却各自为政 + 启发式(近因)。
1. **价值感知的、长生命周期(小时/天)缓存生命周期** —— 人人都按 LRU/LFU/S3FIFO 驱逐;
   **没人**按 `P(reuse)×recompute_cost×SLO_value` 做准入/驱逐/TTL。最强的差异点;
   = 长生命周期复用目标。*(我们的 MVP)*
2. **缓存亲和性 ↔ 负载均衡** 作为一个联合的在线最优(DualMap/GORGO 表明这仍是开放问题)。
3. **默认的非前缀 / 部分复用**(CacheBlend/CacheGen 证明可行;只有 LMCache 暴露了它)。
4. **在线复用 vs 重算** 的成本模型,按块/层,对标 PCIe↔HBM 落差。
5. **一致的多引擎全局 KV 命名空间** —— *放弃*(我们是单引擎 vLLM)。
6. MoE wide-EP 的 KV 协同调度;跨区域复用;热块复制(Mooncake:>50% 的块从不被复用,少数
   命中数万次 → 复制是必需的)。

**护城河** = KV 之上的策略大脑(每字节 goodput),而不是又一个传输引擎/层。

## E. 这驱动出的决策
- 单一引擎 = **vLLM**(治理/生态 + KV 复用的提升空间)。
- 自建 = 树外插件(见 `02-vllm-kv-hookpoints.md`);挂载 NIXL/Mooncake/LMCache。
- MVP = 价值感知的长生命周期(缺口 #1)。

## 来源(代表性)
引擎:vLLM V1 博客 + PyTorch-Foundation 文章;SGLang RadixAttention/HiCache(LMSYS)+ RadixArk;
TRT-LLM v1.0 PyTorch 后端;TGI 归档。控制平面:Dynamo(NVIDIA)、llm-d(Red Hat)、AIBrix
(arXiv 2504.03648)、KServe(CNCF)、Gateway API Inference Extension。KV:Mooncake(FAST'25,
arXiv 2407.00079)、DistServe(OSDI'24,2401.09670)、CacheBlend(EuroSys'25,2405.16444)、
LMCache(arXiv 2510.09665)、DualMap(2602.06502)、GORGO(2602.11688)、KVFlow(2507.07400)、
MLA/Wide-EP(vLLM 博客 2025-12-17)。*(逐条论断的完整引用见底层调研报告。)*

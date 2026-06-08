# 09 — 工业级真实推理主体(GLM-4.5-Air / 355B × 8×A100 80GB)

本篇以「把大型 Dense/MoE 在多节点、高并发下稳定服务」作为项目地基(`docs/00` 主体定位)。
此前调研偏重 KV 策略(`docs/01/02/03`),对推理主体本身(模型×硬件可行性、并行、量化、
长上下文、SLO)覆盖不足。本篇补齐这部分,并贴合目标硬件:8×A100 80GB PCIe、driver 550 /
CUDA 12.4(Ampere sm80,非 Hopper)。所有关键数字附一手来源;无法确认的标注「待核实/估计」。

## ① 主体定位
项目不重造引擎(`docs/01`:vLLM 已商品化)。主体定义为:把已下载的 **GLM-4.5-Air(MoE,106B 总 /
12B 激活)** 与 **GLM-4.5(355B 总 / 32B 激活)** 在 8×A100 上以正确的并行 + 量化 + 调度跑到
「能服务」的状态,作为 KV/MTP 优化的承载平台与约束来源。
> 架构数字来源:GLM-4.5 技术报告 arXiv 2508.06471(355B/32B、106B、MoE、23T tokens);
> HF 模型卡 `zai-org/GLM-4.5-Air`(106B 总 / 12B 激活、MIT、BF16 权重)。

贯穿全篇的硬约束:A100 是 Ampere(sm80),无原生 FP8 算力。这一点决定了下面整张可行性矩阵,详见 ④。

## ② 模型 × 硬件可行性矩阵(A100 80GB)
权重显存估算:BF16 ≈ 2 字节/参数。Air 106B → ≈212 GB;355B → ≈710 GB(均不含 KV/激活/通信缓冲)。
8×A100 = 640 GB 总显存。

| 模型 | 精度路径(A100) | 权重显存 | 8×A100 单机? | 推荐起步配置 | 备注 |
|---|---|---|---|---|---|
| **GLM-4.5-Air**(106B/12B) | **BF16**(FP8 不可用,见④) | ≈212 GB | 可(212 GB ≪ 640 GB,余 ≈400 GB 给 KV) | `TP=8` 或 `DP=2,TP=4 + --enable-expert-parallel` | 官方示例 `TP=8`(FP8 卡)/`TP=4`;A100 改 BF16 权重 |
| **GLM-4.5**(355B/32B) | **BF16** | ≈710 GB | 不可(710 GB > 640 GB) | ≥2 节点(16×A100)`PP=2 × TP=8`,或 wide-EP 跨节点 | 单机 8 卡放不下;FP8(8×H100)路径 A100 不可用 |

- **GLM-4.5-Air 官方 vLLM 命令**(FP8 卡):`vllm serve zai-org/GLM-4.5-Air-FP8 --tensor-parallel-size 8 --tool-call-parser glm45 --reasoning-parser glm45 --enable-auto-tool-choice`;
  官方亦提供 `TP=4` 版本。A100 上须改用 BF16 权重 `zai-org/GLM-4.5-Air`(FP8 在 Ampere MoE 上不可用,见④)。
  来源:vLLM Recipes GLM 页 <https://docs.vllm.ai/projects/recipes/en/latest/GLM/GLM.html>;vLLM 博客 <https://vllm.ai/blog/2025-08-19-glm45-vllm>。
- **355B 多节点**:业界基准是 8×H100 / 4×H200(FP8)或 16×H100 / 8×H200(BF16)。A100 无 FP8,
  对应 BF16 档位,即 ≥16×A100(2 节点)。来源(BF16/FP8 GPU 档位):GMICloud GLM 部署指南
  <https://www.gmicloud.ai/en/blog/where-to-run-glm-5-inference-in-the-cloud-gpu-requirements-deployment-options-and-scaling-considerations>(第三方,待核实,但与权重显存算术一致)。
- **显存吃紧时的相关参数**(官方推荐):`--gpu-memory-utilization=0.95`、`--max-model-len=65536`(默认上限 128k)、
  `--max-num-batched-tokens=32768`;H100 上 BF16 放不下时用 `--cpu-offload-gb 16`(同理适用于 A100)。
  来源:vLLM Recipes GLM 页(同上)。
- **PCIe 互联约束**:目标服务器为 A100 80GB PCIe(非 SXM/NVLink),TP 的 all-reduce 走 PCIe
  带宽,远低于 NVLink,因此大 TP(尤其跨 8 卡)通信开销显著,倾向 `TP≤4` + DP/EP 组合而非纯 `TP=8`(估计,需实测;
  PCIe↔NVLink 落差量级见 `docs/01` C:GPU↔CPU ~32–63 GB/s vs HBM ~3–8 TB/s)。

## ③ 并行维度权衡(TP / PP / EP / DP / attention-DP / wide-EP)
vLLM 将 MoE 的并行拆为两套独立的切分方式:attention 层与 expert 层可用不同策略。核心等式:`EP_SIZE = TP_SIZE × DP_SIZE`。
来源:vLLM EP 部署文档 <https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/>;DP 部署文档 <https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/>。

| 维度 | 切什么 | 通信模式 | 适用 | 对 TTFT/TPOT/吞吐 | vLLM 开关 |
|---|---|---|---|---|---|
| **TP** 张量并行 | 每层权重按列/行切 N 份 | 每层 all-reduce(同步、频繁) | 单节点、显存放不下;NVLink 友好 | 降单请求延迟;PCIe 上 all-reduce 成本高,A100 PCIe 慎用大 TP | `--tensor-parallel-size` |
| **PP** 流水线并行 | 按层分段到不同卡/节点 | 段间点对点(稀疏) | 跨节点装下超大模型(355B) | 加 TTFT(流水线填充气泡);吞吐靠 micro-batch 摊薄 | `--pipeline-parallel-size` |
| **DP** 数据并行 | 整模型复制,独立批 | 几乎无(各副本独立) | 水平扩吞吐;每副本独立 KV | 高并发吞吐线性扩;不降单请求延迟 | `--data-parallel-size` |
| **attention-DP** | MoE 中 attention 层走 DP | 仅 expert 层需同步 | MoE(尤其 MLA 模型,KV 只来自 attention) | 降 attention 冗余、利于前缀缓存命中 | `TP=1` 时 attention 在 DP 间复制;`TP>1` 时 attention 走 TP |
| **EP** 专家并行 | experts 分散到 EP ranks,token 路由分发 | All-to-All(EP 组内) | MoE expert 层;比把 MoE 当 dense 切 TP 更高效 | 提 MoE 吞吐 + 局部性;All-to-All 成为新瓶颈 | `--enable-expert-parallel` |
| **wide-EP** | EP 跨多节点铺开(DeepEP/pplx kernel) | 跨节点 All-to-All | 超大 MoE 多节点(355B) | 多节点解放 expert 显存;依赖高速互联 | `--all2all-backend deepep_high_throughput`(prefill)/`deepep_low_latency`(decode) |

**关键事实与取舍:**
- 不加 `--enable-expert-parallel` 时,MoE 层退化为大小 `TP×DP` 的 TP 组(当 dense 切);加上后 expert 层走 EP,
  对 MoE 更高效且局部性更好。来源:EP 文档(同上)。
- **DP+EP 单机范例**(可借鉴到 Air):`vllm serve deepseek-ai/DeepSeek-V3-0324 --tensor-parallel-size 1 --data-parallel-size 8 --enable-expert-parallel`
  → 单节点 8 路 EP。来源:EP 文档(同上)。
- **attention-DP / per-rank KV(对 KV 策略是硬约束)**:每个 DP engine 持有独立 KV cache(`docs/02` 已记:
  MoE KV 按 DP rank 分区,无全局视图),因此任何跨 rank/全局 KV 策略(`docs/00` B7)= connector + 共享哈希存储,而非单块管理器。
  来源:DP 文档 + `docs/02`。
- **DP 的同步代价**:前向必须对齐;即便某 rank 请求数少于 DP 路数,expert 层每次前向仍须全员同步,负载不均时会产生空转。来源:DP 文档(同上)。
- **All-to-All 后端按 prefill/decode 区分**:`deepep_high_throughput`(prefill 主导)vs `deepep_low_latency`(decode 主导);
  DeepEP kernel 对混合负载表现差,仅 P/D 分离场景才发挥。可叠加 `--enable-dbo`(通信/计算重叠)、`--async-scheduling`(实验)。来源:EP 文档(同上)。

> **A100 配置取向(估计,需实测):** Air 单机首选 `DP=2,TP=4` + EP 或 `TP=8`;PCIe 无 NVLink,倾向 `TP≤4` 减少 all-reduce。
> 355B 跨 2 节点首选 `PP=2 × TP=8`(段间点对点更省跨节点带宽)或 wide-EP;wide-EP 跨节点 All-to-All 在 PCIe+以太/IB 上代价高,需 benchmark 定夺。

## ④ 量化(A100 的 FP8 硬限制)
这是本篇最关键的约束。vLLM 官方明确:
> "FP8 computation is supported on NVIDIA GPUs with compute capability >= 8.9 (Ada Lovelace, Hopper)."
> 来源:vLLM FP8 W8A8 文档 <https://docs.vllm.ai/en/latest/features/quantization/fp8/>。

A100 = sm80 < 8.9,因此 A100 没有原生 FP8(W8A8)算力。A100 上 FP8 的可用路径如下:

| 量化路径 | A100(sm80)可用? | 机制 / 限制 |
|---|---|---|
| **FP8 W8A8**(权重+激活 FP8 算) | 不可用 | 需 cc≥8.9。A100 报错或静默回退 |
| **FP8 W8A16**(仅权重 FP8,激活 FP16,Marlin) | dense 可 / MoE 不可 | 官方:Turing/Ampere 支持 W8A16 weight-only FP8(Marlin)。但 MoE 例外 |
| **FP8 MoE on Ampere** | 不可用 | vLLM 的 FusedMoE 用 Triton kernel,不支持 FP8 Marlin 混合精度 GEMM,GLM-4.5-Air-FP8 在 A100 上跑不起来,须 dequant 回 FP16 |
| **AWQ / GPTQ / INT4**(Marlin/Machete) | 可用 | Marlin/Machete 自定义 kernel 对 Ampere(A100+)与 Hopper 都加速 |
| **FP8 KV cache**(*存储*量化,非算力) | 可用(与算力 FP8 不同) | `kv_cache_dtype=fp8` 是 KV *存储*格式,e4m3/e5m2 仅需 CUDA 11.8+ |

来源:FP8 W8A8 文档(同上);sglang issue #12887 + vLLM issue #17579(Ampere MoE FP8 经 Triton FusedMoE 不支持 FP8 Marlin,须回 float16)
<https://github.com/sgl-project/sglang/issues/12887> / <https://github.com/vllm-project/vllm/issues/17579>;
GLM-4.5-Air-FP8 在 Ampere(RTX3090)实测报错 `size_n ... not divisible by tile_n_size` <https://github.com/zai-org/GLM-4.5/issues/94>;
GPTQModel 文档(Marlin/Machete 覆盖 Ampere)<https://docs.vllm.ai/en/latest/features/quantization/gptqmodel/>;
KV cache 量化文档(e4m3 CUDA11.8+/ROCm、e5m2 CUDA11.8+)<https://docs.vllm.ai/en/latest/features/quantization/quantized_kvcache/>。

**实操结论(A100):**
- GLM-4.5-Air 在 A100 上运行采用 BF16(106B→≈212 GB,8 卡放得下);不使用 `-FP8` 变体(MoE FP8 在 Ampere 不可用)。
- 若需压缩权重显存(尤其想单机塞 355B 的子集或多副本):走 AWQ/GPTQ INT4(Marlin,Ampere 支持),而非 FP8。
- **FP4 / NVFP4**:Blackwell 专属,A100 不可用(具体 vLLM 报错待核实,但 cc 远低于 Blackwell sm100)。

**FP8 KV cache(A100 可用,与 KV 主题强相关):**
- 2× KV 显存压缩(vs BF16);长上下文(MRCR)恢复 97–98% baseline 到 128k;推理基准退化至多 1–2 分。
- decode ITL 斜率降到 BF16 的 54%(Llama-3.1-8B)/71%(gpt-oss-20b);~7k token 为盈亏平衡点,
  <7k 上下文 BF16 反而略快;并发 8 时吞吐 +14.9%。
  来源:vLLM FP8 KV-cache 博客(2026-04)<https://vllm.ai/blog/2026-04-22-fp8-kvcache>(注:该文以 Hopper/Blackwell 测,A100 数值待自测)。
- **投机解码 × KV 量化的兼容性约束**:某些 spec 路径(如 DFlash)与任何 KV 量化(fp8_e5m2/e4m3/turboquant)不兼容,锁死 bf16 KV(非因果 attention 需求)。
  这对 C1 MTP × FP8 KV cache 的组合是直接约束,须验证 GLM MTP 是否受限。来源:vLLM issue #41559 <https://github.com/vllm-project/vllm/issues/41559>。

> **量化对长上下文 / 投机接受率的净判断:** FP8 KV cache 是 A100 上唯一与算力无关的省 KV 手段,长上下文友好(97–98%);
> 但短上下文不划算,且可能与 MTP 接受率路径冲突。这是 Foretoken 要量化清楚的输入(见⑦)。

## ⑤ 长上下文成本
- **Prefill 计算随长度的标度**:attention 是 O(n²),TTFT 随 prompt 长度线性甚至更快增长,是长上下文的主导延迟瓶颈。
  来源:Spheron LLM serving 优化 <https://www.spheron.network/blog/llm-serving-optimization-continuous-batching-paged-attention/>;SpecPrefill 论文 arXiv 2502.02789(背景印证)。
- **chunked prefill(vLLM V1 默认开)**:把长 prefill 切块、与 decode 交织,削平长请求挤占交互请求时的 TTFT 尖峰。
  相关参数:`max-num-batched-tokens` 调小改善 ITL(prefill 少打断 decode);调大改善 TTFT(每批 prefill 更多)。
  来源:同上 Spheron;vLLM 优化文档 <https://docs.vllm.ai/en/v0.8.2/performance/optimization.html>。
- **长上下文 KV 显存墙**:V1 中 KV 容量需求 ≈ `max-num-seqs × max-model-len`(token 数)。`docs/01` 已记量级:
  70B @ 1M token → ≈135 GB KV(> 权重)。GLM-4.5-Air @ 128k 在 8×A100 BF16 下,KV 会快速吃掉那 ≈400 GB 余量,因此必须 FP8 KV cache + 限 `max-model-len`。
  来源:vLLM 参数指南 <https://medium.com/@kaige.yang0110/vllm-throughput-optimization-1-basic-of-vllm-parameters-c39ace00a519>;`docs/01`。
- **cache-miss 重算的代价量级**:重算 = 重跑 O(n²) prefill;PCIe↔HBM 落差 ~50–100×(`docs/01`)主导「复用 vs 重算」决策。
  这是 `docs/00` B5(复用 vs 重算成本模型)的物理基础:长 prompt 下重算代价巨大,偏向缓存/卸载复用(前提是 P(reuse) 足够,见 `docs/06`)。

## ⑥ 高并发 / SLO 服务
- **continuous batching**:vLLM 默认;`max-num-seqs` 调大,高负载下 RPS 升;`max-num-batched-tokens` 调大,偏 TPS 而非 RPS。
- **batch>128 / 高并发的尾延迟特征**:并发拉高让 GPU 满载、RPS 升,但过载会同时推高 TTFT / ITL / E2E;
  须分别监控 p50/p95/p99,p50 稳但 p99 恶化是队列堆积/显存压力的早期信号。
- **SLO 下的吞吐拐点(knee)**:吞吐(TPS/RPS)反映「做了多少工」,goodput 反映「多少工达标 SLO」;
  现代引擎 batching 下约 25–35k tok/s,但高并发下 goodput 约 70–80%(达标率,第三方量级,待核实)。
  来源:Hivenet continuous batching 指南 <https://compute.hivenet.com/post/continuous-batching-explained>;
  TianPan TTFT/throughput 分解 <https://tianpan.co/blog/2026-03-10-llm-latency-decomposition-ttft-vs-throughput>;Anyscale 指标文档 <https://docs.anyscale.com/llm/serving/benchmarking/metrics>。
- goodput 即 `docs/00` D4 / 统一标尺:per-GPU 字节秒 goodput 是所有优化的裁判;高并发拐点是评测台(`docs/07`)要刻画的曲线。

## ⑦ 对 Foretoken 的含义(直接 Use vLLM / 作为模块的输入与约束)
**直接 Use(主体地基,不自建):**
- **引擎执行 / 并行 TP·PP·EP·DP·attention-DP·wide-EP**(`docs/00` A1/A3)= 全用 vLLM 配置面,零代码、零 fork。
- **MoE FusedMoE / 量化 kernel / chunked prefill / continuous batching**(A2/A4/D1)= Use。
- **MTP(C1)**:GLM-4.5 系列自带 MTP 头,vLLM 经 `--speculative-config.method mtp --num_speculative_tokens 1` 启用;
  `num_speculative_tokens=1` 时接受率常 >90%,调大到 3 接受率下降、净吞吐反降。因此 C1 直接 Use,C2(自适应 K)才是项目的空位。
  来源:vLLM Recipes GLM 页 + MTP 文档 <https://docs.vllm.ai/en/latest/features/speculative_decoding/mtp/>。

**作为优化模块的输入 / 硬约束:**
1. **A100 无 FP8 算力 + MoE FP8 on Ampere 不可用**:主体一律 BF16 权重 +(可选)AWQ/GPTQ INT4;KV 省显存只能靠 FP8 *KV cache*(存储)+ 卸载/驱逐策略,不能依赖 FP8 权重。这是 B2/B3/B8 全部工作的显存背景。
2. **per-DP-rank 独立 KV(无全局视图)**:B7 跨 rank 策略 = connector + 共享哈希存储(`docs/02` 已锁)。
3. **长上下文 KV 显存墙(128k 下 KV 吞掉余量)+ O(n²) 重算代价**:B5(复用 vs 重算)、B8(P(reuse))的收益正比于 prompt 长度;长上下文是策略最划算的场景。
4. **FP8 KV cache <7k 不划算 + 可能与某些 spec 路径冲突**:C2/B3 必须把「是否启用 FP8 KV」「draft KV 是否量化」纳入决策,且校验 GLM MTP × FP8 KV 是否兼容(`docs/00` 已记 MTP draft KV 不可卸载契约)。
5. **PCIe(非 NVLink)互联**:大 TP all-reduce 成本高;goodput 模型须含通信项;实测拐点是评测台一等公民。
6. **goodput / p99 拐点** = D4 与统一标尺的经验目标曲线(`docs/04/07`)。

## ⑧ 开放问题(待实测 / 待核实)
- **Air 单机最优并行**:`TP=8` vs `DP=2,TP=4+EP` 在 A100 PCIe 上谁的 goodput 高?(PCIe all-reduce 代价无公开数,须自测。)
- **355B on 2×A100 节点**:`PP=2×TP=8` vs wide-EP,跨节点互联(IB/以太?)未知,可行性与吞吐待实测(连「装得下」都要先验证 KV 余量)。
- **FP8 KV cache 在 A100 上的真实数值**:官方博客以 Hopper/Blackwell 测,A100 的 ITL 斜率/盈亏点须自测(服务器已有 `glm_longctx`/`longctx_real_result` 日志可用)。
- **GLM MTP × FP8 KV cache 是否兼容**:DFlash 类已知冲突;GLM `glm4_moe_mtp` 路径是否受限未核实,C1↔KV 量化契约要落实。
- **AWQ/GPTQ INT4 对 GLM-4.5-Air MoE 的质量损失 + 对 MTP 接受率的影响**:无 GLM 专项数据,须自测(服务器有 `glm_lossless`/`glm_humaneval` 可对比)。
- **355B 在 A100 上是否值得**:若 BF16 强制 ≥16 卡 2 节点、PCIe 通信吃掉收益,主体可先聚焦 Air,355B 留作压力测试。

## 来源清单(本篇实际使用)
**vLLM 官方文档 / 博客:**
- GLM Recipes:<https://docs.vllm.ai/projects/recipes/en/latest/GLM/GLM.html>
- GLM-4.5 × vLLM 博客:<https://vllm.ai/blog/2025-08-19-glm45-vllm>
- Expert Parallel 部署:<https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/>
- Data Parallel 部署:<https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/>
- FP8 W8A8 量化(cc≥8.9 硬要求):<https://docs.vllm.ai/en/latest/features/quantization/fp8/>
- KV cache 量化:<https://docs.vllm.ai/en/latest/features/quantization/quantized_kvcache/>
- GPTQModel(Marlin/Machete on Ampere):<https://docs.vllm.ai/en/latest/features/quantization/gptqmodel/>
- MTP:<https://docs.vllm.ai/en/latest/features/speculative_decoding/mtp/>
- FP8 KV-cache 博客(2026-04):<https://vllm.ai/blog/2026-04-22-fp8-kvcache>
- 性能优化:<https://docs.vllm.ai/en/v0.8.2/performance/optimization.html>

**GLM / 模型一手:**
- GLM-4.5 技术报告 arXiv 2508.06471:<https://arxiv.org/abs/2508.06471>
- HF `zai-org/GLM-4.5-Air`(106B/12B、BF16、MIT):<https://huggingface.co/zai-org/GLM-4.5-Air>
- HF `zai-org/GLM-4.5-Air-FP8`:<https://huggingface.co/zai-org/GLM-4.5-Air-FP8>

**Ampere FP8 / MoE 限制(关键证据):**
- vLLM issue #17579(MoE FP8 Marlin 支持请求):<https://github.com/vllm-project/vllm/issues/17579>
- sglang issue #12887(Ampere MoE FP8 须回 float16):<https://github.com/sgl-project/sglang/issues/12887>
- GLM-4.5 issue #94(Ampere FP8 实测报错):<https://github.com/zai-org/GLM-4.5/issues/94>
- vLLM issue #41559(spec decode × KV 量化不兼容):<https://github.com/vllm-project/vllm/issues/41559>

**长上下文 / 并发 / SLO(含第三方,标「待核实」):**
- Spheron LLM serving 优化:<https://www.spheron.network/blog/llm-serving-optimization-continuous-batching-paged-attention/>
- Hivenet continuous batching:<https://compute.hivenet.com/post/continuous-batching-explained>
- TianPan TTFT vs throughput:<https://tianpan.co/blog/2026-03-10-llm-latency-decomposition-ttft-vs-throughput>
- Anyscale 指标:<https://docs.anyscale.com/llm/serving/benchmarking/metrics>
- vLLM 参数指南(KV 容量公式):<https://medium.com/@kaige.yang0110/vllm-throughput-optimization-1-basic-of-vllm-parameters-c39ace00a519>
- 355B GPU 档位(第三方,待核实):<https://www.gmicloud.ai/en/blog/where-to-run-glm-5-inference-in-the-cloud-gpu-requirements-deployment-options-and-scaling-considerations>

> **硬件信息范围**:本篇只写硬件类型(A100 80GB PCIe / driver 550 / CUDA 12.4);内网 IP / 路径 / 账号等敏感信息不入本仓库。

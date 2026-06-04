# 07 —— 评测实操手册(贴近真实业务的 playbook)

`docs/04` 是评测的**理论与原则**(为什么测 goodput、防作弊陷阱、gate-zero);本篇是**落地清单**——
具体用哪个 trace、敲什么命令、报哪几张图、对标谁,将来照着直接跑。命令参数以你装的版本
`vllm bench serve --help` / 各工具文档为准。

> **核心信条:"像真实业务" = 真实时间戳回放(突发性)+ 真实长度分布 + 真实前缀复用结构
> (hash 字节级一致)。三者缺一,KV 评测就失真。**
> 反面铁证:随机 token 会把命中率**假性打到 0**;朴素拼接"到达 trace + 数据集"会**误判容量 ~50%**
> (ServeGen, NSDI'26);复用局部性强 workload-dependent(KVCache in the Wild, ATC'25:to-C 80%
> 复用 <10 分钟、to-B <10 秒)——合成负载复刻不出来。

> **⚠️ 重心(2026-06 纠偏):项目主体是"真实推理",验证以"真实模型 + 现成真实任务 benchmark"为
> 主线** —— SWE-bench / AIME / GPQA / LiveCodeBench / 长文档 QA(有内容、现成、贴 2026 业务、**零
> 自录**),用真实模型(GLM-4.5-Air)端到端量 TTFT/TPOT/goodput + **KV 与 MTP 等优化模块**的真实
> 提升。**下方的 trace 回放 + dummy 模型法,降级为"快速隔离 KV 机制、省 GPU 时"的辅助,不是主线;
> 且不自录 trace。** MTP 评测本就必须真模型 + 真内容(见 §3)。

## 0.5 评测方案总纲(统一:一套真实场景 + 4 对照配置)
**不分"KV 评测 / MTP 评测"两套——就一套**:在同一份真实场景负载上跑 4 个开关配置,KV/MTP 是其中
被评的优化、**不改评测本身**。
- **负载(一套)**:§6.5 缝合法 —— Mooncake 骨架(时序/并发/轮次/复用)+ prompt pool 填内容 +
  **真实模型现场生成**,带真实时序/并发/复用/内容。**全程真模型**(要测 MTP 接受率,不能 dummy)。
- **4 个对照配置**(同一负载,只切优化开关):**① 原生 vLLM(全关=基线)② 只开 KV ③ 只开 MTP
  ④ 全开** → 对比即可拆出 **KV 与 MTP 各自的贡献 + 端到端总效果**(这是为了"归因",不是分成多套评测)。
- **量**:端到端 goodput / TTFT / TPOT / 显存;拆 KV(命中率、每字节 goodput)+ MTP(接受率、加速);
  每个配置加**无损校验**(输出 == 原生 vLLM)。
- **dummy 仅作可选加速**:想快速扫一大堆 KV 缓存容量点(纯机制隔离、省 GPU)时,可临时用
  Mooncake + dummy 跑那个子实验;**主线评测就是上面这套统一的真实场景**。

## 1. 用什么 trace(为缝合法提供时序/并发/复用骨架)
| trace | 时间戳 | 长度分布 | 前缀复用 hash | 用途 | 来源 |
|---|---|---|---|---|---|
| **Mooncake trace**(★主选) | ✅ ms | ✅ in 891–126k | ✅ **512-block `hash_ids`** | 多轮/agentic/长上下文全覆盖 | HF `valeriol29/mooncake-traces` |
| BurstGPT | ✅ + session | ✅ | ❌ | 真实突发到达 / 会话边界 | github HPMLL/BurstGPT |
| Azure LLM 2023/24 | ✅ | ✅ | ❌ | 到达 + 长度建模 | github Azure/AzurePublicDataset |
| LMSYS-Chat-1M / ShareGPT | ❌ | ✅(从文本算) | ❌(真实多轮→可自构前缀) | 真实语义多轮前缀 | arXiv 2309.11998 |
| kv-cache-tester(739 Claude Code) | ✅ 60min | ✅ 20k→115k | ✅ block hashes | agentic 深评 | github callanjfox/kv-cache-tester |

→ **主线用 Mooncake trace**(子集:`conversation` 多轮 / `toolagent` agentic / 长上下文),它是
**唯一开箱带 `hash_ids` 的大规模生产 trace**。schema:`{timestamp_ms, input_length,
output_length, hash_ids[]}`;`hash_ids` 按 **512-token 块**滚动前缀哈希,相同 id ⇒ 该块及全部
前导块 token 相同 ⇒ KV 可复用。

## 2. 用什么 harness(命令)
**主力 = vLLM 自带 `bench serve`(同源、保真):**
```bash
vllm bench serve \
  --backend vllm --model <同架构模型> \
  --dataset-name timed_trace --dataset-path <mooncake>.jsonl \
  --self-timed \                       # 按 trace 真实时间戳发(保突发性)
  --timed-trace-chunk-hash-size 512 \  # ⚠️ 必须=Mooncake 块大小,否则复用边界错位
  --timed-trace-sec-multiplier 0.001   # ms→s
```
**辅助 / 交叉验证:**
- SGLang `bench_serving --dataset-name mooncake --use-trace-timestamps`(+ `generated-shared-prefix
  --gsp-group-distribution zipf --gsp-zipf-alpha ...` 建模前缀热度幂律——评驱逐很有用);
- NVIDIA **AIPerf**(GenAI-Perf 继任;原生 goodput;`--custom-dataset-type mooncake_trace`);
- **Vidur 模拟器**:没 GPU 时先扫"容量×策略×并发"大网格(成本 ~$10 vs 真机 ~$218K),再真机验证少数点。

## 3. 保真:主线全程真模型(dummy 仅作可选加速)+ 一个必对齐的参数
**主线是 §0.5 那套统一的真实场景评测,全程真模型。** 下表说明的是"为什么 KV 那部分*理论上*可用
dummy 加速、而 MTP 绝不能"——**dummy 只是想快速扫 KV 容量时的可选辅助,不是独立的一套评测**:

| 评什么 | 模型 | 输入 | 关键指标 | 为什么 |
|---|---|---|---|---|
| **KV 管理(P1)**:驱逐/卸载/复用 | dummy(同架构随机权重)即可 | `hash` 占位 token | hit-rate / goodput / 每字节 goodput | 命中率由 token 一致性决定、延迟由**架构**决定,**与权重无关** |
| **MTP / 投机解码(P2+)** | **必须真实权重**(GLM-4.5-Air 等) | **必须真实/语义真实文本**(ShareGPT/LMSYS/真实 agentic) | **接受率 / 平均接受长度 / TPS 加速比** | 接受率**完全取决于真实预测质量 + 输入真实语义**;dummy + 占位 token 测出来是随机、无意义的 |

- **KV 管理评测 —— dummy 模型法**(vLLM×Mooncake 官方做法):**同架构 dummy 模型** + 让**相同
  `hash_id` 生成相同占位 token** → 前缀字节级一致(命中行为真)+ prefill/decode 计算时序真
  (KV 大小/FLOPs 真)+ 隐私 & 可复现。**绝不用随机 token。**
- **MTP/投机评测 —— 必须真实模型**:用 GLM-4.5-Air(`Glm4MoeMTP`,A100 可跑)等**真实权重** +
  **真实文本输入**(占位 token 不行,接受率依赖内容);指标与 regime 图见 `docs/05`。
- ⚠️ **块大小对齐**:Mooncake 块=**512**,vLLM 默认 prefix 块=**16**。回放必须显式
  `--timed-trace-chunk-hash-size 512`,否则重建的复用边界与引擎缓存块边界错位 → 命中率失真。

## 4. 报什么(指标 + 图)
1. **goodput**(首要,非裸 throughput)= 在 ≥90% 请求同时满足 **TTFT_P90≤a× & TPOT_P90≤b×** SLO
   下的最大 req/s;**只算完整完成的请求**。
2. **hit-rate vs 缓存容量 曲线**(横轴 ×HBM,纵轴命中率)。
3. **reuse-distance 分桶命中率**(<10s / <10min / >10min)。
4. **TTFT / TPOT 的 p50/p95/p99**(分开报;缓存主要影响 TTFT)。
5. **★ goodput-per-GPU-byte**(你的核心卖点,需自研采集)—— 每字节 KV 的边际价值 / 固定 goodput
   下的 HBM 节省。业界未标准化,正是差异化。

> **硬约束**:GQA + LRU 下缓存到 **~2×HBM** 就逼近无限容量命中率 → **必须在 <2×HBM 的受限区间打**,
> 否则 LRU 已够好、显不出 value-aware。

## 5. 对标谁(baselines)
- **LRU**(必备,主要击败对象)+ **Belady 离线最优**(上界;报"闭合 LRU→Belady gap 的比例")。
- 真竞品(见 `docs/03`):**T-LRU(RFC #37823)、SAECache、LMCache、SGLang HiCache**。
- 方法论模板(直接抄):
  - **Tail-Optimized Caching**(arXiv 2510.15152):以**尾延迟**为目标对标 LRU/Threshold-LRU/Belady;
    报 P90/95/99 TTFT + SLO 违约率(它实测 P90 尾 TTFT −27.5%、SLO 违约 −38.9%,代价中位延迟升高)。
  - **Don't Break the Cache**(arXiv 2601.06007):agentic 里动态工具结果**污染缓存反增延迟** →
    构造"动态内容污染"对照组,证明你的 value 函数能识别 + 降权一次性内容(这是相对纯 prefix-caching
    的差异化证据)。

## 6. 分场景负载构造
- **多轮对话**:Mooncake `conversation` / LMSYS-Chat;复用 = 第 N+1 轮前缀 = 第 N 轮全文(逐轮增长)。
- **agentic coding**:Mooncake `toolagent` / kv-cache-tester;复用 = 巨大稳定系统提示 + 工具定义
  (~12–20k token 共享前缀;Claude Code 系统提示实测 20k+,LMCache)+ 累积历史。**必测失败模式**:动态工具结果不该缓存(Don't Break the Cache)。
- **RAG**(最脆):检索文档每次顺序不同、破坏简单前缀;构造"同文档集不同顺序"暴露问题;不处理
  position-independent 复用(CacheBlend/CacheClip 路线)就明确划 out-of-scope。
- **长上下文**:Mooncake input 直到 126k 天然覆盖;分层缓存(GPU/CPU/SSD)参考 Strata(2508.18572)。

## 6.5 ★负载缝合法:Mooncake 骨架 + prompt pool(一套负载同时压 KV 和 MTP)
**动机**:贴 2026 业务的真实数据有个死结——有真实时序/复用的 trace(Mooncake/Inferact)**无内容**;
有真实内容的(benchmark/对话集)**无时序/并发**。**缝合二者**即可破局:**Mooncake 当骨架(时序/并发/
轮次/间隔/长度),prompt pool 当血肉(真实内容),模型真实生成回复**。产出的一套多轮负载**同时满足
KV(时序+复用+并发)和 MTP(真实内容)**,且不用自录。

> **⚠️ 单独跑 benchmark 时 KV/MTP 分工不同**(`docs/10` 用一手长度数据核实):**AIME / GPQA /
> LiveCodeBench**(短输入 + 超长 output + 无跨题共享前缀)主压 **MTP 接受率**,几乎压不出 KV
> 复用;**SWE-bench-Verified(agent 模式)**(巨型共享系统前缀 + 累积历史)主压 **KV 复用**。所以
> "一套负载同时压 KV+MTP"靠的正是**缝合**(Mooncake 时序骨架注入复用结构),而非任一 benchmark
> 天然"通吃"。

**各部分来源:**
| 维度 | 来自 |
|---|---|
| 到达时间戳 / 并发 / 会话轮次 / 轮间间隔 / 每轮长度 | **Mooncake trace(骨架)** |
| 每轮真实文本内容 | **prompt pool(血肉)**:现成 benchmark prompt / LMSYS 多轮 / **`lightseekorg/kimi-mtp-dataset`**(47.6万行多轮 ShareGPT,正对口 MTP)抽 prompt |
| 每轮回复 | **真实模型现场生成**(MTP 要测接受率,必须真生成) |

**流程:**
1. 从 Mooncake trace 抽**会话骨架**:每会话的起始时刻、轮数 N、各轮 input/output 长度、轮间间隔。
2. 为每会话从 pool 取**一个连贯的多轮序列**(轮数 ≈ N)填充各轮 user 输入;**所有会话用同一份系统提示**(见下"复用")。
3. 按骨架时序**多会话并发回放**:每轮把 user 输入 + **累积历史**喂真实模型生成回复,轮间按间隔 sleep;多会话的到达/轮次在全局时间轴上交错 = 真实并发。
4. 量:KV(命中率 / goodput / 每字节 goodput)+ MTP(接受率 / 加速)+ **无损校验**(输出 == 原生 vLLM)。

**三个落地要点(保真命门):**
- **复用结构**:① **会话内**(第 N+1 轮吃前 N 轮历史)—— 同会话同序列填,**字节一致累积、自然真实**
  (KV 复用的大头);② **跨会话**(共享前缀,主要是系统提示/工具定义)—— 用 pool 填后默认会丢,
  **让所有会话填同一份系统提示**即可保留。
- **长度对齐**:Mooncake 每轮有指定长度,pool prompt 不一定匹配 → **挑长度接近的 prompt 填**,或放宽
  (以 Mooncake 时序/轮次为准、长度由内容自然定)。
- **输出真生成**:输出必须真模型生成(MTP 要),Mooncake 的 `output_length` 只作 `max_tokens` 参考/
  上限,**不塞假输出**。

**实用权衡(别过度对齐)**:严格复刻 Mooncake 每个 `hash_id` 的复用模式工作量大、无必要。**取 Mooncake
最珍贵的"时序 + 并发 + 轮次节奏"当骨架;复用靠"会话内多轮历史"自然产生 + "跨会话统一系统提示"对齐**,
即抓住主要矛盾。

> **分工**:纯 KV 评测用 Mooncake + dummy 更省(无需真模型);**本缝合法的价值在 KV+MTP 端到端**
> (需要真实内容 + 真模型现场生成)。

## 6.6 数据源确认 + 缝合实现(2026-06,已落地 `bench/stitch.py` + `bench/replay.py`)

**数据源(已核实真实 HF id / vLLM 支持):**
| 角色 | 用什么 | vLLM 怎么吃 |
|---|---|---|
| 时序/并发/复用**骨架** | **Mooncake trace**(`timestamp/input_length/output_length/hash_ids`,**无 session id**) | `--dataset-name timed_trace`(真正的 trace 引擎) |
| 真实内容**血肉** | **`lightseekorg/kimi-mtp-dataset`**(47.6万行多轮 ShareGPT,Apache-2.0;Kimi-K2.5 生成,正对口 MTP) | 经缝合 → custom / 自写回放 |
| 纯 MTP benchmark | AIME=`AI-MO/aimo-validation-aime`(vLLM `AIMODataset` 直吃);GPQA=`Idavidrein/gpqa`(gated)/ LCB=`livecodebench/code_generation_lite` → **转 custom jsonl**;SWE-bench=`princeton-nlp/SWE-bench_Verified` → **agent 镜像** | `hf` / `custom` |

**两个确认死扣:**
- vLLM `--dataset-name` 15 个里,**真正带时序的"trace"只有 `timed_trace` 与 `burstgpt`**;其余是合成(random/sonnet/prefix_repetition)或无时序数据集。vLLM 按 `SUPPORTED_DATASET_PATHS` **白名单**分派 HF 数据集,**不是任意 HF 都吃**。
- **Mooncake 无 session/user id(隐私)** —— 多轮/会话**全藏在 `hash_ids` 前缀包含**里(相同 hash = 该块及之前 token 全同 = 可复用)。

**"一套全真"(真实内容 + 真实复用 + 真实多用户并发)只有三档:**
| 档 | 做法 | 多用户并发 | 让步 |
|---|---|---|---|
| 自录 agent trace | 真 agent scaffold 跑真任务录请求 | ❌ **单会话串行,造不出并发** | 重;并发缺失=致命 → 出局 |
| **Mooncake-hash 缝真实块(主选)** | 每个 `hash_id` 绑定固定真实文本块(相同 hash→同块)→ 复用 100% 复刻 Mooncake | ✅ 真(Mooncake) | 块边界偶不连贯;要自写回放器 |
| 会话累积 + Mooncake 节奏 | 真实多轮对话累积(会话内自然复用)+ 借 Mooncake 时序(= §6.5 法) | ✅ 真(Mooncake) | 复用非复刻 Mooncake、靠对话本身 |
→ **并发只有真实生产 trace 才有,自录给不了 → 主选「Mooncake-hash 缝真实块」(第二档),代码只实现它**;第三档(会话累积 / §6.5)作文档备选、不落代码。

**已落地实现(只主线 B,A 已砍):**
- `bench/stitch.py`(模式 B):`fill_mooncake_trace`(hash→真实块)+ `build_block_pool`/`extract_texts`
  + main(Mooncake loader + GLM tokenizer + 写带 `timestamp` 的 trace)。
- `bench/replay.py`:按真实 `timestamp` 异步回放 + 采 TTFT/TPOT + `goodput_per_gpu_byte_second`。
- **为什么砍 A**:A 路径(对话累积 + `--request-rate` 合成到达)**没真实多用户并发**,是退化版;它仅有
  的两个用途都有更好替代——要真实并发 → B;纯 MTP benchmark(不在乎到达)→ 直接 `vllm bench serve
  --dataset-name hf/sharegpt`(vLLM 原生)。**B 通吃 KV+MTP,A 两头不靠 → 砍。**
- **诚实账单**:走 B 全真 = 放弃 vLLM bench serve、自己重造「发请求/采集/goodput」最小子集
  (`timed_trace` 只认 hash、`custom` 没 timestamp,都吃不了"真实 prompt + 真实到达")。

**KV 评测为何必须真实 trace(不能只 `--request-rate`)**:① 请求**顺序**直接决定命中(`--request-rate` 全局泊松、不分会话、打散复用);② 我们的**时间感知策略**(`P(reuse|Δt)`/TTL/卸载)吃**绝对间隔**;③ 真实**并发**决定瞬时 KV 压力。三者 Mooncake `timed_trace`(或 B 路径回放)都真。

## 7. 开跑前 checklist(gate)
- [ ] **gate-zero(回放保真)**:Mooncake 经**原生 vLLM APC** 回放,实测 token 级命中率 ≈ 离线从
      `hash_ids` 算的命中率(差几个百分点内);否则 hash→token 重建坏了,所有数无效。
- [ ] **块大小对齐**:`--timed-trace-chunk-hash-size 512` 已设;校验"相同 hash_id 的请求确实命中
      同一组缓存块"。
- [ ] **内存压力**:GPU KV 容量设到 **工作集 > 容量**(否则不驱逐 = 策略没被测)。
- [ ] **受限容量区间**:在 **<2×HBM** 打。
- [ ] **各 arm 一致**:trace/seed/容量/模型/并行/CUDA-graph/warmup/SLO 全相同,只有策略不同;≥3 seed + 95% CI。
- [ ] **CUDA-graph 一致**:别拿 eager-baseline 比 graph-treatment。
- [ ] **报 goodput 不是 throughput**;只算完整完成的请求;TTFT/TPOT 分开报。

## 8. 一句话配方
> 用 **Mooncake trace**(带 `hash_ids`,覆盖多轮/agentic/长上下文)+ vLLM `bench serve` 的
> `timed_trace` 自计时回放(**块大小对齐 512**)作主线,SGLang/AIPerf 交叉验证;主报 **goodput、
> p99 TTFT、hit-rate-vs-capacity 曲线**,对标 **LRU 与 Belady**;自研 **goodput-per-GPU-byte** 作为
> value-aware 核心卖点;再用 BurstGPT/Azure 注入真实突发、用 kv-cache-tester + "Don't Break the
> Cache" 式负载做 agentic 深评。**三要素缺一(真实时间戳/真实长度/真实前缀复用),评测就失真。**

## 来源(代表性)
Mooncake trace(HF `valeriol29/mooncake-traces`)+ FAST'25 2407.00079 · vLLM×Mooncake blog(2026-05-06,
加速数为官方自报) · ServeGen NSDI'26 2505.09999 · KVCache in the Wild ATC'25 2506.02634 · BurstGPT
KDD'25 2401.17644 · Azure/AzurePublicDataset · LMSYS-Chat-1M 2309.11998 · vLLM Benchmark CLI 文档 ·
SGLang bench_serving 文档 · NVIDIA AIPerf(ai-dynamo) · Vidur MLSys'24 2405.05465 · DistServe OSDI'24
2401.09670(goodput 定义) · Tail-Optimized Caching 2510.15152 · Don't Break the Cache 2601.06007 ·
ML-Based GPU Caching 2509.20979(**preprint,非 FAST'25**)· CacheClip 2510.10129 · Strata 2508.18572 ·
kv-cache-tester(github callanjfox)· LMCache MI300X agentic blog(2026-05-12)。

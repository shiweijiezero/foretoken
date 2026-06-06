# 07 —— 评测实操手册

`docs/04` 给出评测的理论与原则(为什么测 goodput、防作弊陷阱、gate-zero);本篇是落地清单:
具体用哪个 trace、敲什么命令、报哪几张图、对标谁。命令参数以所装版本的
`vllm bench serve --help` / 各工具文档为准。

> 核心原则:"像真实业务" = 真实时间戳回放(突发性)+ 真实长度分布 + 真实前缀复用结构
> (hash 字节级一致)。三者缺一,KV 评测即失真。
> 依据:随机 token 会把命中率假性打到 0;朴素拼接"到达 trace + 数据集"会误判容量 ~50%
> (ServeGen, NSDI'26);复用局部性强 workload-dependent(KVCache in the Wild, ATC'25:to-C 80%
> 复用 <10 分钟、to-B <10 秒),合成负载复刻不出来。

> 评测重心:项目主体是真实推理,验证以"真实模型 + 现成真实任务 benchmark"为主线——
> SWE-bench / AIME / GPQA / LiveCodeBench / 长文档 QA(有内容、现成、贴 2026 业务、零
> 自录),用真实模型(GLM-4.5-Air)端到端量 TTFT/TPOT/goodput + KV 与 MTP 等优化模块的真实
> 提升。下方的 trace 回放 + dummy 模型法用于快速隔离 KV 机制、节省 GPU 时,作辅助;不自录
> trace。MTP 评测必须用真模型 + 真内容(见 §3)。

## 0.5 评测方案总纲(一套真实场景 + 4 对照配置)
KV 与 MTP 共用同一套评测:在同一份真实场景负载上跑 4 个开关配置,KV/MTP 是其中
被评的优化,不改评测本身。
- **负载(一套)**:§6.5 缝合法——Mooncake 骨架(时序/并发/轮次/复用)+ prompt pool 填内容 +
  真实模型现场生成,带真实时序/并发/复用/内容。全程真模型(测 MTP 接受率不能用 dummy)。
- **4 个对照配置**(同一负载,只切优化开关):① 原生 vLLM(全关=基线)② 只开 KV ③ 只开 MTP
  ④ 全开 → 对比可拆出 KV 与 MTP 各自的贡献 + 端到端总效果,用于归因。
- **量**:端到端 goodput / TTFT / TPOT / 显存;拆 KV(命中率、每字节 goodput)+ MTP(接受率、加速);
  每个配置加无损校验(输出 == 原生 vLLM)。
- **dummy 作可选加速**:需快速扫大量 KV 缓存容量点(纯机制隔离、省 GPU)时,可临时用
  Mooncake + dummy 跑该子实验;主线评测为上述统一的真实场景。

## 1. 用什么 trace(为缝合法提供时序/并发/复用骨架)
| trace | 时间戳 | 长度分布 | 前缀复用 hash | 用途 | 来源 |
|---|---|---|---|---|---|
| **Mooncake trace**(主选) | ✅ ms | ✅ in 891–126k | ✅ **512-block `hash_ids`** | 多轮/agentic/长上下文全覆盖 | HF `valeriol29/mooncake-traces` |
| BurstGPT | ✅ + session | ✅ | ❌ | 真实突发到达 / 会话边界 | github HPMLL/BurstGPT |
| Azure LLM 2023/24 | ✅ | ✅ | ❌ | 到达 + 长度建模 | github Azure/AzurePublicDataset |
| LMSYS-Chat-1M / ShareGPT | ❌ | ✅(从文本算) | ❌(真实多轮→可自构前缀) | 真实语义多轮前缀 | arXiv 2309.11998 |
| kv-cache-tester(739 Claude Code) | ✅ 60min | ✅ 20k→115k | ✅ block hashes | agentic 深评 | github callanjfox/kv-cache-tester |

主线用 Mooncake trace(子集:`conversation` 多轮 / `toolagent` agentic / 长上下文),它是
唯一开箱带 `hash_ids` 的大规模生产 trace。schema:`{timestamp_ms, input_length,
output_length, hash_ids[]}`;`hash_ids` 按 **512-token 块**滚动前缀哈希,相同 id 表示该块及全部
前导块 token 相同,KV 可复用。

## 2. 用什么 harness(命令)
主力为 vLLM 自带 `bench serve`(同源、保真):
```bash
vllm bench serve \
  --backend vllm --model <同架构模型> \
  --dataset-name timed_trace --dataset-path <mooncake>.jsonl \
  --self-timed \                       # 按 trace 真实时间戳发(保突发性)
  --timed-trace-chunk-hash-size 512 \  # 必须=Mooncake 块大小,否则复用边界错位
  --timed-trace-sec-multiplier 0.001   # ms→s
```
辅助 / 交叉验证:
- SGLang `bench_serving --dataset-name mooncake --use-trace-timestamps`(+ `generated-shared-prefix
  --gsp-group-distribution zipf --gsp-zipf-alpha ...` 建模前缀热度幂律——评驱逐很有用);
- NVIDIA **AIPerf**(GenAI-Perf 继任;原生 goodput;`--custom-dataset-type mooncake_trace`);
- **Vidur 模拟器**:没 GPU 时先扫"容量×策略×并发"大网格(成本 ~$10 vs 真机 ~$218K),再真机验证少数点。

## 3. 保真:主线全程真模型,dummy 作可选加速;一个必对齐的参数
主线为 §0.5 那套统一的真实场景评测,全程真模型。下表说明 KV 部分可用 dummy 加速、而 MTP 不可:
dummy 仅作快速扫 KV 容量时的可选辅助,不是独立的一套评测。

| 评什么 | 模型 | 输入 | 关键指标 | 为什么 |
|---|---|---|---|---|
| **KV 管理(P1)**:驱逐/卸载/复用 | dummy(同架构随机权重)即可 | `hash` 占位 token | hit-rate / goodput / 每字节 goodput | 命中率由 token 一致性决定、延迟由**架构**决定,**与权重无关** |
| **MTP / 投机解码(P2+)** | **必须真实权重**(GLM-4.5-Air 等) | **必须真实/语义真实文本**(ShareGPT/LMSYS/真实 agentic) | **接受率 / 平均接受长度 / TPS 加速比** | 接受率**完全取决于真实预测质量 + 输入真实语义**;dummy + 占位 token 测出来是随机、无意义的 |

- **KV 管理评测——dummy 模型法**(vLLM×Mooncake 官方做法):同架构 dummy 模型 + 让相同
  `hash_id` 生成相同占位 token → 前缀字节级一致(命中行为真)+ prefill/decode 计算时序真
  (KV 大小/FLOPs 真)+ 隐私与可复现。不用随机 token。
- **MTP/投机评测——必须真实模型**:用 GLM-4.5-Air(`Glm4MoeMTP`,A100 可跑)等真实权重 +
  真实文本输入(占位 token 不行,接受率依赖内容);指标与 regime 图见 `docs/05`。
- **块大小对齐**:Mooncake 块=**512**,vLLM 默认 prefix 块=**16**。回放必须显式
  `--timed-trace-chunk-hash-size 512`,否则重建的复用边界与引擎缓存块边界错位,命中率失真。

## 4. 报什么(指标 + 图)
1. **goodput**(首要,非裸 throughput)= 在 ≥90% 请求同时满足 **TTFT_P90≤a× & TPOT_P90≤b×** SLO
   下的最大 req/s;**只算完整完成的请求**。
2. **hit-rate vs 缓存容量 曲线**(横轴 ×HBM,纵轴命中率)。
3. **reuse-distance 分桶命中率**(<10s / <10min / >10min)。
4. **TTFT / TPOT 的 p50/p95/p99**(分开报;缓存主要影响 TTFT)。
5. **goodput-per-GPU-byte**(核心指标,需自研采集):每字节 KV 的边际价值 / 固定 goodput
   下的 HBM 节省。业界未标准化,是本项目的差异化指标。

> 硬约束:GQA + LRU 下缓存到 **~2×HBM** 即逼近无限容量命中率,故必须在 <2×HBM 的受限区间评测,
> 否则 LRU 已足够好,显不出 value-aware 的差异。

## 5. 对标谁(baselines)
- **LRU**(必备,主要对标对象)+ **Belady 离线最优**(上界;报"闭合 LRU→Belady gap 的比例")。
- 同类竞品(见 `docs/03`):**T-LRU(RFC #37823)、SAECache、LMCache、SGLang HiCache**。
- 方法论模板:
  - **Tail-Optimized Caching**(arXiv 2510.15152):以尾延迟为目标对标 LRU/Threshold-LRU/Belady;
    报 P90/95/99 TTFT + SLO 违约率(实测 P90 尾 TTFT −27.5%、SLO 违约 −38.9%,代价为中位延迟升高)。
  - **Don't Break the Cache**(arXiv 2601.06007):agentic 中动态工具结果污染缓存反增延迟。
    构造"动态内容污染"对照组,验证 value 函数能识别并降权一次性内容(相对纯 prefix-caching
    的差异化证据)。

## 6. 分场景负载构造
- **多轮对话**:Mooncake `conversation` / LMSYS-Chat;复用 = 第 N+1 轮前缀 = 第 N 轮全文(逐轮增长)。
- **agentic coding**:Mooncake `toolagent` / kv-cache-tester;复用 = 大型稳定系统提示 + 工具定义
  (~12–20k token 共享前缀;Claude Code 系统提示实测 20k+,LMCache)+ 累积历史。需测的失败模式:动态工具结果不应缓存(Don't Break the Cache)。
- **RAG**:检索文档每次顺序不同,破坏简单前缀;构造"同文档集不同顺序"暴露问题;不处理
  position-independent 复用(CacheBlend/CacheClip 路线)则明确划为 out-of-scope。
- **长上下文**:Mooncake input 直到 126k 天然覆盖;分层缓存(GPU/CPU/SSD)参考 Strata(2508.18572)。

## 6.5 负载缝合法:Mooncake 骨架 + prompt pool(一套负载同时压 KV 和 MTP)
动机:贴 2026 业务的真实数据存在缺口——有真实时序/复用的 trace(Mooncake/Inferact)无内容;
有真实内容的(benchmark/对话集)无时序/并发。缝合二者可同时满足两侧:Mooncake 作骨架(时序/并发/
轮次/间隔/长度),prompt pool 作内容(真实内容),模型真实生成回复。产出的一套多轮负载同时满足
KV(时序+复用+并发)和 MTP(真实内容),且不用自录。

> 单独跑 benchmark 时 KV/MTP 分工不同(`docs/10` 用一手长度数据核实):AIME / GPQA /
> LiveCodeBench(短输入 + 超长 output + 无跨题共享前缀)主压 MTP 接受率,几乎压不出 KV
> 复用;SWE-bench-Verified(agent 模式)(大型共享系统前缀 + 累积历史)主压 KV 复用。因此
> "一套负载同时压 KV+MTP"依赖缝合(Mooncake 时序骨架注入复用结构),而非任一 benchmark
> 天然覆盖两者。

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

**三个保真要点:**
- **复用结构**:① 会话内(第 N+1 轮吃前 N 轮历史)——同会话同序列填,字节一致累积、自然真实
  (KV 复用的主要来源);② 跨会话(共享前缀,主要是系统提示/工具定义)——用 pool 填后默认会丢,
  让所有会话填同一份系统提示即可保留。
- **长度不对齐**:不对齐 Mooncake 的逐请求长度,按轮数匹配对话(以 Mooncake 时序/轮次为准,长度
  由 kimi 内容自然定)。
- **输出真生成**:输出必须真模型生成(MTP 需要),回放给统一 `max_tokens` 上限并自然 EOS,不预设
  每条输出长度、不塞假输出。

**实用权衡**:严格复刻 Mooncake 每个 `hash_id` 的复用模式工作量大且无必要。取 Mooncake
的"时序 + 并发 + 轮次节奏"作骨架;复用靠"会话内多轮历史"自然产生 + "跨会话统一系统提示"对齐。

> 分工:纯 KV 评测用 Mooncake + dummy 更省(无需真模型);本缝合法的价值在 KV+MTP 端到端
> (需要真实内容 + 真模型现场生成)。

## 6.6 评测负载:会话重建缝合

将 Mooncake trace 的会话与时序结构,与真实多轮对话的内容结合,得到同时具备真实复用、真实并发与真实内容的负载,用于在同一份负载上评测 KV 管理与 MTP。实现见 `data_prepare/`(缝合 / 打包)与 `bench/`(回放)。

### 数据源
| 角色 | 数据集 |
|---|---|
| 会话 / 时序 / 并发骨架 | Mooncake trace —— `valeriol29/mooncake-traces`(字段 `timestamp` / `input_length` / `output_length` / `hash_ids`;无 session id) |
| 内容 | `lightseekorg/kimi-mtp-dataset`(47.6 万行多轮 ShareGPT,Apache-2.0) |
| 纯 MTP benchmark(可选) | AIME `AI-MO/aimo-validation-aime`(vLLM `AIMODataset` 直读);GPQA `Idavidrein/gpqa`(gated);LiveCodeBench `livecodebench/code_generation_lite`;SWE-bench `princeton-nlp/SWE-bench_Verified` |

### 前提
- vLLM `--dataset-name` 中仅 `timed_trace` 与 `burstgpt` 自带真实时序;HF 数据集按 `SUPPORTED_DATASET_PATHS` 白名单分派,并非任意 HF 数据集均可直接使用。
- Mooncake 无 session/user id;会话关系由 `hash_ids` 的前缀包含隐式编码(相同 hash 表示该块及其前序 token 完全相同,可复用)。

### 方法
1. `reconstruct_sessions`:按 `hash_ids` 的最长前缀延续链重建会话——请求 B 是请求 A 的后续轮,当且仅当 A 的完整 `hash_ids` 是 B 的真前缀。仅共享开头(如系统提示)不视为同一会话,以避免误并。
2. `fill_sessions`:打乱对话池,为每个会话(M 轮)选取一条轮数不少于 M 的对话逐轮填入;每轮 prompt 为前 k 轮的累积,timestamp 取自该轮对应的 Mooncake 记录。长度按轮数粗对齐,不对齐 `input_length`;不预设输出长度,回放给统一 `max_tokens` 上限并自然 EOS。

不复刻 Mooncake 的 512 块 hash 复用:该粒度取决于 Kimi 生产环境的块大小、缓存与内容,与本项目的 vLLM(16 块)/ GLM 配置不一致。复用关系由内容决定、与缓存配置无关——真实多轮对话本身即产生会话内累积复用与跨会话系统提示共享,并具备价值分层(系统提示、会话历史、一次性内容),即价值感知策略的评测对象。内容连贯亦使 MTP 接受率不受块边界影响。

### 实现(`data_prepare/` 缝合打包 + `bench/` 回放)
- `data_prepare/make_workload.py`:`reconstruct_sessions` / `fill_sessions` / `to_turns`。
- `data_prepare/build_dataset.py`:打包为 parquet 与 dataset card,供 `load_dataset` 直接复用。
- `bench/replay.py`:按 `timestamp` 异步回放,采集 TTFT/TPOT,计算每 GPU 字节秒 goodput。
- 回放自建的原因:vLLM `timed_trace` 仅接受 hash、`custom` 不含 timestamp,二者均无法同时承载真实 prompt 与真实到达时刻。

### KV 评测须使用真实 trace
请求顺序决定缓存命中;时间感知策略(`P(reuse|Δt)`、TTL、卸载)依赖绝对时间间隔;真实并发决定瞬时 KV 压力。`--request-rate` 为全局泊松到达且不区分会话,无法满足上述任一条件。

## 7. 开跑前 checklist(gate)
- [ ] **gate-zero(回放保真)**:Mooncake 经原生 vLLM APC 回放,实测 token 级命中率 ≈ 离线从
      `hash_ids` 算的命中率(差几个百分点内);否则 hash→token 重建有误,数据无效。
- [ ] **块大小对齐**:`--timed-trace-chunk-hash-size 512` 已设;校验"相同 hash_id 的请求确实命中
      同一组缓存块"。
- [ ] **内存压力**:GPU KV 容量设到工作集 > 容量(否则不驱逐,策略未被测)。
- [ ] **受限容量区间**:在 <2×HBM 评测。
- [ ] **各 arm 一致**:trace/seed/容量/模型/并行/CUDA-graph/warmup/SLO 全相同,只有策略不同;≥3 seed + 95% CI。
- [ ] **CUDA-graph 一致**:不用 eager-baseline 比 graph-treatment。
- [ ] **报 goodput 而非 throughput**;只算完整完成的请求;TTFT/TPOT 分开报。

## 8. 一句话配方
> 用 Mooncake trace(带 `hash_ids`,覆盖多轮/agentic/长上下文)+ vLLM `bench serve` 的
> `timed_trace` 自计时回放(块大小对齐 512)作主线,SGLang/AIPerf 交叉验证;主报 goodput、
> p99 TTFT、hit-rate-vs-capacity 曲线,对标 LRU 与 Belady;自研 goodput-per-GPU-byte 作为
> value-aware 的核心指标;再用 BurstGPT/Azure 注入真实突发、用 kv-cache-tester + "Don't Break the
> Cache" 式负载做 agentic 深评。三要素缺一(真实时间戳/真实长度/真实前缀复用),评测即失真。

## 来源(代表性)
Mooncake trace(HF `valeriol29/mooncake-traces`)+ FAST'25 2407.00079 · vLLM×Mooncake blog(2026-05-06,
加速数为官方自报) · ServeGen NSDI'26 2505.09999 · KVCache in the Wild ATC'25 2506.02634 · BurstGPT
KDD'25 2401.17644 · Azure/AzurePublicDataset · LMSYS-Chat-1M 2309.11998 · vLLM Benchmark CLI 文档 ·
SGLang bench_serving 文档 · NVIDIA AIPerf(ai-dynamo) · Vidur MLSys'24 2405.05465 · DistServe OSDI'24
2401.09670(goodput 定义) · Tail-Optimized Caching 2510.15152 · Don't Break the Cache 2601.06007 ·
ML-Based GPU Caching 2509.20979(**preprint,非 FAST'25**)· CacheClip 2510.10129 · Strata 2508.18572 ·
kv-cache-tester(github callanjfox)· LMCache MI300X agentic blog(2026-05-12)。

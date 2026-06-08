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
- **负载(一套)**:会话重建缝合(§6.6)——Mooncake trace 提供真实时序/并发/会话结构,真实多轮
  对话集提供内容,真实模型现场生成回复,带真实时序/并发/复用/内容。全程真模型(测 MTP 接受率不能用 dummy)。
- **4 个对照配置**(同一负载,只切优化开关):① 原生 vLLM(全关=基线)② 只开 KV ③ 只开 MTP
  ④ 全开 → 对比可拆出 KV 与 MTP 各自的贡献 + 端到端总效果,用于归因。
- **量**:端到端 goodput / TTFT / TPOT / 显存;拆 KV(命中率、每字节 goodput)+ MTP(接受率、加速);
  每个配置加无损校验(输出 == 原生 vLLM)。
- **dummy 作可选加速**:需快速扫大量 KV 缓存容量点(纯机制隔离、省 GPU)时,可临时用
  Mooncake + dummy 跑该子实验;主线评测为上述统一的真实场景。

## 1. 用什么 trace(为会话重建缝合提供时序/并发/复用结构)
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

## 2. 怎么跑(闭环回放,三种后端)
主线 harness 是项目自带的**闭环回放**(`bench/replay.py`),非 `vllm bench serve`——后者 `timed_trace`
仅接受 hash、`custom` 不含 timestamp,均无法同时承载真实 prompt + 真实到达时刻;且闭环要求下一轮 prompt
用模型**现场回复**(非预录答案)。回放逻辑(时间调度 / 会话化 / 现场拼下一轮)三种后端一致,区别在请求发往
哪、测到哪一层——各测什么 / 何时用见 [README「三种后端形式」](../README.md)。

```bash
# bench.sh 设好引擎环境(CUDA_HOME / PATH),其余参数原样透传给 replay 的 CLI
# 1) 进程内(默认):自起 AsyncLLM、退出释放 GPU,测纯引擎 + 逐 iteration KV/并发
CUDA_VISIBLE_DEVICES=0 HF_HOME=<cache> bash scripts/bench.sh \
  --model <weights|HF id> --config config/models/<model>.toml \
  --split conversation --window 0:10 --rate 20
# 2) 打已有 vllm serve:--endpoint http://host:8000 --gpus <服务器卡数>
# 3) 自起 vllm serve、跑完整组 kill 释放 GPU:--serve --dp 4 --api-server-count 4
# 全部参数与默认值:python -m foretoken.bench.replay --help
```
- `--config` 配置文件(`[sampling]` 官方采样 + `[serve]` 引擎,见 `docs/14`);`--dataset` 默认 HF `foretoken-trace`,也可接本地 `.jsonl`/`.parquet`/目录。
- **负载匹配硬件(关键)**:真实 trace 是集群级到达,单实例(如 4×A100)1× 全量回放必然过载(TTFT 飙到分钟级)。`--rate R`(req/min,自动换算 total_requests=R×窗口分钟)或 `--total-requests N` 会话级下采样到该 request 量(整会话保留 = 负载均衡分给本实例的份额),或 `--sec-multiplier` 拉伸时间;扫不同 `--rate` 出 goodput-vs-load 曲线、拐点即可持续容量。
- `--window N|A:B`(分钟)截时间片;`--deadline SEC` / `--tail-factor`(默认 2.0)墙钟上限,到点取消在飞请求(掐长尾——个别高温采样会一路顶到 max_tokens 拖垮整轮);`--slo TTFT_ms:TPOT_ms`(可重复)定 goodput 达标阈值;`--tag` 标优化变体(`vllm-default`/`kv-aware`/`mtp`);`--param`/`--engine-param` 透传任意 vLLM 采样 / 引擎字段。

### 产出(每 run 一目录,见 §4 指标)
`runs/<时间>__<model>__<tag>__<split>_<window>/`:`run.json`(配置+聚合指标)、`turns.jsonl`(每轮指标原始,
事后换 SLO 重算/画图)、`cases.md`/`cases.jsonl`(每轮输入输出,由 `--cases off|sample|full` 控,默认 sample)、`engine_stats.jsonl`
(逐 iteration 引擎 KV%/在飞/排队)、`summary.md`(markdown 摘要+内嵌图)、`en/`·`zh/`(双语论文级图)。
跨 run:`runs/INDEX.md` 排行榜、`runs/compare/` 对照图;`python -m foretoken.bench.report runs --plots --compare` 可重生成。

### 交叉验证(可选,非主线)
SGLang `bench_serving --dataset-name mooncake --use-trace-timestamps`、NVIDIA **AIPerf**(原生 goodput)、
**Vidur 模拟器**(无 GPU 先扫"容量×策略×并发"大网格,再真机验证少数点)。

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

**已实现(主线每 run 自动出,见 §2 产出;`report/` 生成,en+zh 论文级图)**:
- **延迟**:TTFT / TPOT / **E2E**(端到端)的 p50/p90/p99 + CDF/直方图;尾部比 p99/p50。
- **吞吐**:原始输出 tok/s(及 /GPU)、总(输入+输出)tok/s、完成 req/s;输出吞吐随时间。
- **goodput**(首要,非裸 throughput)= 满足 SLO(TTFT≤a 且 TPOT≤b)的**有效输出 tok/s**,按严/中/松
  SLO 阶梯(`--slo` 可配)报达成% + /GPU + 归一化 **tok/(s·GPU字节)**;只算完整完成的请求。
- **引擎 GPU 监控**(取引擎 `SchedulerStats`,自定义 stat logger):**KV cache 利用率**(峰值/均值 + 随时间)、
  并发(在飞/排队随时间)。
- 输入/输出**长度分布**、TTFT 随到达时刻散点、跨 run 对照 CDF。

**KV 专项(规划,value-aware KV 落地后补)**:
- **hit-rate vs 缓存容量 曲线**(横轴 ×HBM)、**reuse-distance 分桶命中率**(<10s/<10min/>10min)、
  **goodput-per-GPU-byte 的边际价值**(固定 goodput 下的 HBM 节省;业界未标准化,本项目差异化指标)。

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

## 6.5 一套负载同时压 KV 与 MTP(动机)
贴 2026 业务的真实数据存在缺口:有真实时序/复用的 trace(Mooncake)无内容;有真实内容的
(benchmark/对话集)无时序/并发。将两者缝合,可在同一份负载上同时满足 KV(真实时序+复用+并发)
与 MTP(真实内容)两侧,且不用自录。具体方法与实现见 §6.6。

单独跑 benchmark 时 KV 与 MTP 的压测分工不同(`docs/10` 用一手长度数据核实):

- **AIME / GPQA / LiveCodeBench**(短输入 + 超长 output + 无跨题共享前缀)主压 MTP 接受率,几乎
  压不出 KV 复用。
- **SWE-bench-Verified(agent 模式)**(大型共享系统前缀 + 累积历史)主压 KV 复用。

因此"一套负载同时压 KV+MTP"需要缝合真实时序与真实多轮内容,而非依赖任一 benchmark 天然覆盖两者。

缝合负载中的 KV 复用来自两处:① 会话内累积——同一会话第 k 轮的 prompt 为前 k 轮的累积,字节一致、
逐轮增长(KV 复用的主要来源);② 跨会话公共前缀——多个会话共享的系统提示 / 工具定义,经多 config
合并后由各子集自身的公共前缀自然保留。两处复用均由内容决定,不依赖缓存块大小。

> 分工:纯 KV 评测用 Mooncake + dummy 更省(无需真模型);缝合负载的价值在 KV+MTP 端到端
> (需要真实内容 + 真模型现场生成)。

## 6.6 评测负载:会话重建缝合

将 Mooncake trace 的会话与时序结构,与真实多轮对话的内容结合,得到同时具备真实复用、真实并发与真实内容的负载,用于在同一份负载上评测 KV 管理与 MTP。实现见 `data_prepare/`(缝合 / 打包)与 `bench/`(回放)。

### 数据源
| 角色 | 数据集 |
|---|---|
| 会话 / 时序 / 并发结构 | Mooncake trace —— `valeriol29/mooncake-traces`(字段 `timestamp` / `input_length` / `output_length` / `hash_ids`;无 session id) |
| 内容 | `lightseekorg/kimi-mtp-dataset`(47.6 万行多轮 ShareGPT,Apache-2.0) |
| 纯 MTP benchmark(可选) | AIME `AI-MO/aimo-validation-aime`(vLLM `AIMODataset` 直读);GPQA `Idavidrein/gpqa`(gated);LiveCodeBench `livecodebench/code_generation_lite`;SWE-bench `princeton-nlp/SWE-bench_Verified` |

### 前提
- vLLM `--dataset-name` 中仅 `timed_trace` 与 `burstgpt` 自带真实时序;HF 数据集按 `SUPPORTED_DATASET_PATHS` 白名单分派,并非任意 HF 数据集均可直接使用。
- Mooncake 无 session/user id,会话关系由 `hash_ids` 的前缀延续隐式编码。`hash_ids` 按 512-token 块滚动累积哈希;input 几乎不是 512 的整数倍,故每请求尾块不满,下一轮累积会把该尾块填满、其 hash 改变。因此须比"去尾满块前缀"而非完整 `hash_ids`——否则前缀在尾块处断裂,真实多轮会被全部漏判为单轮。

### 方法
1. `reconstruct_sessions`:按去尾满块前缀的延续链重建会话——B 续 A,当 A 的去尾满块前缀是 B 的 `hash_ids` 前缀。两道防误连:① 共享前缀须 ≥ `min_shared_blocks` 块,自适应(自动检测各 config 公共前缀块数:conversation 1 块、mooncake/toolagent 12 块,取其后),排除仅共享公共 system/few-shot 的不同会话;② B 的 timestamp 须严格晚于该会话上一轮,排除同时刻并发请求被并入。
2. `fill_sessions`:打乱对话池,长会话优先取轮数最接近的对话逐轮填入;每轮 prompt 为前 k 轮的累积,timestamp 取自该轮对应的 Mooncake 记录。长度按轮数粗对齐,不对齐 `input_length`;不预设输出长度,回放给统一 `max_tokens` 上限并自然 EOS。
3. 多 config 分 split:`conversation`(对话)、`mooncake`(混合)、`toolagent`(agent/工具)三个真实子集各自自适应重建、各作为一个 split(保留各自真实的 0~1 小时时序,互不串联;按需分场景或合并回放);`synthetic` 为合成负载、无真实多轮,排除。

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
> 用 `foretoken-trace`(Mooncake 时序/会话结构 + 真实多轮内容缝合)经**进程内闭环回放**
> (`bench/replay.py`:真模型现场生成、按真实时间戳、会话级下采样匹配硬件、墙钟时限掐长尾)作主线,
> SGLang/AIPerf 交叉验证;主报 goodput、p99 TTFT/E2E、KV 利用率,对标 LRU 与 Belady;自研
> goodput-per-GPU-byte 作为 value-aware 的核心指标;再用 BurstGPT/Azure 注入真实突发、用
> kv-cache-tester + "Don't Break the Cache" 式负载做 agentic 深评。三要素缺一(真实时间戳/真实长度/
> 真实前缀复用),评测即失真。

## 来源(代表性)
Mooncake trace(HF `valeriol29/mooncake-traces`)+ FAST'25 2407.00079 · vLLM×Mooncake blog(2026-05-06,
加速数为官方自报) · ServeGen NSDI'26 2505.09999 · KVCache in the Wild ATC'25 2506.02634 · BurstGPT
KDD'25 2401.17644 · Azure/AzurePublicDataset · LMSYS-Chat-1M 2309.11998 · vLLM Benchmark CLI 文档 ·
SGLang bench_serving 文档 · NVIDIA AIPerf(ai-dynamo) · Vidur MLSys'24 2405.05465 · DistServe OSDI'24
2401.09670(goodput 定义) · Tail-Optimized Caching 2510.15152 · Don't Break the Cache 2601.06007 ·
ML-Based GPU Caching 2509.20979(**preprint,非 FAST'25**)· CacheClip 2510.10129 · Strata 2508.18572 ·
kv-cache-tester(github callanjfox)· LMCache MI300X agentic blog(2026-05-12)。

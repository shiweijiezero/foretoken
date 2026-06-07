# 14 — 模型推理参数

各模型官方推荐采样参数 + 评测固定值。优先取模型自带 `generation_config.json`(作者官方默认),缺失项以官方文档 / model card 补并标来源。

## 官方推荐(参考,真实质量导向)

| 模型 | temperature | top_p | top_k | max_output | context | 来源 |
|---|---|---|---|---|---|---|
| `zai-org/GLM-4.5-Air` | 1.0 | — | — | 96K | 131072 | gen_config / model card / README / api_request.py / Z.AI 文档均未给固定采样 |
| `mistralai/Mistral-Small-4-119B-2603` | 0.7 / 0.1(精确) | 0.95 | — | ≤ ctx | 1048576 | Mistral docs;gen_config 未设采样 |
| `google/gemma-4-26B-A4B-it` | 1.0 | 0.95 | 64 | 32768 | 262144 | gen_config(权威)+ OpenRouter |

> context / max_output 取自模型 `config.json`(`max_position_embeddings`)与官方 / 平台文档;Mistral max_output 受 `input + output ≤ context` 约束,无单独上限。
> GLM-4.5-Air 官方未给固定采样:`generation_config.json`、HF model card、GitHub README、官方 `inference/api_request.py`(demo 用 `temperature=0.0`/greedy)、[Z.AI 参数文档](https://docs.z.ai/guides/overview/concept-param)(仅给 top_p 推荐区间 0.8–0.95、且建议 temperature/top_p 只调一个)均无确定值,故取 `temperature=1.0`(引擎默认)、top_p 不设。

## context 与 serve 的 max_model_len

`context` 是模型官方能力,`serve.max_model_len` 是实际分配的 KV 窗口,二者不同:`max_model_len` 须同时 `≥ max_tokens` 且装得下显存。本 trace 真实输出 p99 ≈ 6k / max ≈ 8.5k token,远小于官方 context;为"官方"硬开 Mistral 1M / Gemma 256K 会把 KV 占满、并发压到约 1 条,毁掉 goodput 评测意义。故 serve 窗口贴工作负载 + 并发余量取值,官方 context 仅作记录。GLM 官方 context 131072 本身装得下且 ≥ max_tokens,直接用。

显存不足时的手段:`--cpu-offload-gb`(权重→CPU,decode 走 PCIe、吞吐暴跌,与 goodput 相悖,不常态用)、`--kv-cache-dtype fp8`(KV 砍半,最干净的 KV 杠杆)、降 `max_model_len`。活跃序列的 attention KV 必须在 GPU 上,CPU/NVMe KV offload(LMCache)只服务前缀缓存复用,不能把单条超长活跃序列塞进小显存。

## 评测用值

- **性能评测**:用上表官方采样(temperature / top_p / top_k / max_tokens)走**真实采样**,反映真实生产 decode 行为;**固定 seed**(`replay --seed`)保可复现——temp > 0 是随机的,且闭环每轮依赖上一轮输出,不固定 seed 则不可复现。
- **max_tokens**:三模型统一 **32768**——远超真实输出(p99 ≈ 6k / max ≈ 8.5k,故仅封顶不预占 KV),又把单条失控生成从官方 max_output(GLM 96K)砍到 32K;**须 ≤ serve 的 max_model_len**,否则 vLLM 400 拒(早期 GLM 设 96000 > max_model_len 65536 即触发)。
- **回放墙钟上限(掐长尾)**:max_tokens 之外再设时间闸——`replay --tail-factor`(默认 2.0)令整轮 ≤ 窗口跨度 × sec_multiplier × factor,到点取消在飞请求。实测高温采样下个别会话会一路顶到 max_tokens、独占引擎拖垮整轮(window 0:1 曾被一条失控生成拖 12 分钟);时间是对 goodput 才正确的轴,与 max_tokens 双保险。
- **无损校验**:单独用 **temperature = 0**(greedy)跑,验 KV / MTP 开关下输出逐字节一致;与性能评测分开(temp > 0 输出随机,无法逐字节比对)。
- 实现:进程内自起 vLLM 引擎(`AsyncLLM`),无独立 HTTP server、进程退出即释放 GPU。配置文件含 `[sampling]` + `[serve]` 两段,`replay --config <path>` **显式指定文件**(任意位置,不绑定 `config/models/`、不做名字模糊匹配;缺省 `config/models/default.toml`)。覆盖:采样 `--param K=V`、引擎 `--engine-param K=V`(任意 `AsyncEngineArgs`,如 `kv_cache_dtype=fp8`/`cpu_offload_gb=20`);`--seed` 固定可复现。数据 `--dataset` 接 HF id 或本地 `.jsonl`/`.parquet`/目录(schema 见 `load_rows`)。
- 指标:延迟分位(TTFT/TPOT)、**原始吞吐**(输出 tok/s 及 /GPU、req/s)、**goodput**(SLO 达成阶梯:达成% + good tok/s + 归一化);原始 vs goodput 对照即见饱和程度。每 run 落 `runs/<...>/`(run.json / turns.jsonl / summary.md / CDF 图)+ `runs/INDEX.md` 排行榜。

## 来源
- GLM-4.5-Air:模型 `generation_config.json`;[Z.AI 参数文档](https://docs.z.ai/guides/overview/concept-param)。
- Mistral-Small-4:[Mistral Sampling 文档](https://docs.mistral.ai/guides/sampling)、[model card](https://docs.mistral.ai/models/model-cards/mistral-small-4-0-26-03)。
- Gemma-4:模型 `generation_config.json`;[Gemma 文档](https://ai.google.dev/gemma/docs/core)。

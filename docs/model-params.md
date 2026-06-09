# 模型参数

各模型官方推荐采样参数 + 评测固定值。优先取模型自带 `generation_config.json`(作者官方默认),缺失项以官方文档 / model card 补并标来源。

## 官方推荐采样(参考,真实质量导向)

| 模型 | temperature | top_p | top_k | max_output | context |
|---|---|---|---|---|---|
| `zai-org/GLM-4.5-Air` | 1.0 | — | — | 96K | 131072 |
| `mistralai/Mistral-Small-4-119B-2603` | 0.7 / 0.1(精确) | 0.95 | — | ≤ ctx | 1048576 |
| `google/gemma-4-26B-A4B-it` | 1.0 | 0.95 | 64 | 32768 | 262144 |

> context / max_output 取自模型 `config.json`(`max_position_embeddings`)与官方 / 平台文档。模型未给固定采样时取引擎默认(`temperature=1.0`、不设 top_p)。

## context 与 serve 的 max_model_len

`context` 是模型官方能力,`serve` 的 `max_model_len` 是实际分配的 KV 窗口,二者不同:`max_model_len` 须同时 `≥ max_tokens` 且装得下显存。真实评测负载的输出长度通常远小于官方 context;为「官方」硬开极大 context 会把 KV 占满、并发压到约 1 条,毁掉 goodput 评测意义。故 serve 窗口应贴工作负载 + 并发余量取值,官方 context 仅作记录。

显存不足时的手段:`kv_cache_dtype=fp8`(KV 砍半,最干净的杠杆)、降 `max_model_len`、权重 CPU offload(decode 走 PCIe、吞吐暴跌,与 goodput 相悖,不常态用)。活跃序列的 attention KV 必须在 GPU 上;CPU/NVMe KV offload 只服务前缀缓存复用,不能把单条超长活跃序列放进小显存。

## 评测用值

- **性能评测**:用上表官方采样走**真实采样**,反映真实生产 decode 行为;**固定 seed**(`--seed`)保可复现——temp > 0 是随机的,且闭环每轮依赖上一轮输出,不固定 seed 则不可复现。
- **max_tokens**:取远超真实输出的值(仅封顶、不预占 KV),同时把单条失控生成封到合理上限;**须 ≤ serve 的 `max_model_len`**,否则引擎 400 拒。
- **回放墙钟上限(截长尾)**:`max_tokens` 之外再设时间闸——`--tail-grace MIN`(默认 5)令整轮 ≤ 窗口跨度 × sec_multiplier + 宽限,到点取消运行中请求。时间是对 goodput 才正确的轴,与 `max_tokens` 双保险。
- **无损校验**:单独用 `temperature=0`(greedy)跑,验 KV / MTP 开关下输出逐字节一致;与性能评测分开(temp > 0 输出随机,无法逐字节比对)。

## 实现

配置文件含 `[sampling]`(采样)+ `[serve]`(引擎)两段,`--config <path>` 显式指定(任意位置;缺省 `config/models/default.toml`)。覆盖:采样 `--param K=V`、引擎 `--engine-param K=V`(任意引擎字段,如 `kv_cache_dtype=fp8` / `cpu_offload_gb=20`);`--seed` 固定可复现。数据 `--dataset` 接 HF id 或本地 `.jsonl` / `.parquet` / 目录。

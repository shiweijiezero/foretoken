# 14 — 模型推理参数

各模型官方推荐采样参数 + 评测固定值。优先取模型自带 `generation_config.json`(作者官方默认),缺失项以官方文档 / model card 补并标来源。

## 官方推荐(参考,真实质量导向)

| 模型 | temperature | top_p | top_k | max_output | context | 来源 |
|---|---|---|---|---|---|---|
| `zai-org/GLM-4.5-Air` | 1.0 | — | — | 96K | 131072 | gen_config 未设采样;z.ai 文档 |
| `mistralai/Mistral-Small-4-119B-2603` | 0.7 / 0.1(精确) | 0.95 | — | ≤ ctx | 1048576 | Mistral docs;gen_config 未设采样 |
| `google/gemma-4-26B-A4B-it` | 1.0 | 0.95 | 64 | 32768 | 262144 | gen_config(权威)+ OpenRouter |

> context / max_output 取自模型 `config.json`(`max_position_embeddings`)与官方 / 平台文档;Mistral max_output 受 `input + output ≤ context` 约束,无单独上限。

## 评测用值

- **性能评测**:用上表官方采样(temperature / top_p / top_k / max_tokens)走**真实采样**,反映真实生产 decode 行为;**固定 seed**(`replay --seed`)保可复现——temp > 0 是随机的,且闭环每轮依赖上一轮输出,不固定 seed 则不可复现。
- **max_tokens**:取各模型官方 max_output(上表),自然 EOS 提前停、不截断(实测 reasoning 输出 p99 ≈ 6k token,远低于上限)。
- **无损校验**:单独用 **temperature = 0**(greedy)跑,验 KV / MTP 开关下输出逐字节一致;与性能评测分开(temp > 0 输出随机,无法逐字节比对)。
- 实现:`config/models/<model>.toml`(每模型一文件)→ `params_for` → replay 按 `--model` 自动取;常用项 `--temperature/--top-p/--top-k/--max-tokens/--seed`、或 `--param K=V`(任意 vLLM 采样参数,如 `repetition_penalty`/`min_p`)可命令行覆盖。

## 来源
- GLM-4.5-Air:模型 `generation_config.json`;[Z.AI 参数文档](https://docs.z.ai/guides/overview/concept-param)。
- Mistral-Small-4:[Mistral Sampling 文档](https://docs.mistral.ai/guides/sampling)、[model card](https://docs.mistral.ai/models/model-cards/mistral-small-4-0-26-03)。
- Gemma-4:模型 `generation_config.json`;[Gemma 文档](https://ai.google.dev/gemma/docs/core)。

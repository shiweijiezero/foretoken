<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Compare reference and candidate models

English | [简体中文](distribution-comparison_zh.md) · [Quality evaluation](README.md)

Add `--reference` to `foretoken eval` to compare a candidate with a reference. By default, the comparison uses identical text prefixes and reports full-vocabulary KL divergence, Top-1/Top-k agreement, and logit differences. To compare the tokens the models actually generate, use [greedy generation](#compare-greedy-generated-sequences).

## Compare a quantized model

Use the [quantized-model examples](../../../examples/quantized-model/README.md) with a source-installed Foretoken platform and their configured model storage. From the repository root:

```bash
foretoken eval examples/quantized-model/bitsandbytes \
  --reference examples/quantized-model/bf16 \
  --output local,plot
```

Both deployments use Qwen2.5-0.5B-Instruct with BF16 computation; the candidate loads its weights in 4-bit. Model and tokenizer settings come from the reference deployment. Existing deployments are reused; temporary deployments run sequentially and are removed after use, so one available GPU is sufficient when both are temporary.

## Read the results

The summary compares candidates using these metrics:

| Metric | Interpretation |
| --- | --- |
| Mean, median, p99 KL | `KL(reference || candidate)` over the complete vocabulary, in nats; lower is closer |
| Top-1 agreement | Fraction of positions with the same most-probable token |
| Top-k overlap | Shared top-k tokens divided by k; defaults to k=5 and 10, configurable with `--top-k` |
| Corpus-token probability change | Candidate minus reference probability for the original text token; mean shows direction, RMS shows magnitude |
| Centered-logit RMSE | Root-mean-square difference after subtracting each vector's mean log probability; invariant to a common logit offset |
| Total variation | Half the sum of absolute probability differences |

Use `distribution_comparison_candidates.csv` for the candidate summary, `distribution_comparison_positions.jsonl` to inspect individual positions, and `plots/` for the comparison figures.

These plots compare Qwen3-0.6B BF16 and bitsandbytes 4-bit through existing endpoints, using two 96-token WikiText-2 windows and scoring their last 32 positions:

![Nominal weight precision compared by KL, logit RMSE, and Top-1 agreement](../imgs/distribution-comparison-weight-bits.png)

![KL and centered-logit RMSE across 64 scored positions](../imgs/distribution-comparison-positions.png)

Use [task evaluation](README.md) for answer quality and [performance evaluation](../perf/README.md) for serving speed.

## Choose the comparison sample

The default corpus is [WikiText-2](https://huggingface.co/datasets/Salesforce/wikitext), configuration `wikitext-2-raw-v1`, test split. Four non-overlapping 512-token windows are sampled, scoring the last 16 positions of each: 64 paired positions per candidate. Each prefix comes from the original corpus rather than generated answers; this is teacher forcing.

Use `--dataset corpus.txt` for local text, or `--dataset corpus.jsonl` for records containing a `text` field. `--text-column` selects another field. Hugging Face datasets also accept `--dataset-config` and `--split`. Adjust sample size with `--context-length`, `--num-windows`, and `--score-tokens`.

## Compare several candidates

List candidate Kustomize deployments directly before the options for the usual case:

```bash
foretoken eval examples/quantized-model/bitsandbytes examples/quantized-model/bf16 \
  --reference examples/quantized-model/bf16 --output local,wandb,plot
```

Each candidate is compared against the reference. To give candidates custom labels or size coordinates, use a [candidate file](../../../examples/quantized-model/candidates.jsonl):

```bash
foretoken eval \
  --reference examples/quantized-model/bf16 \
  --candidates examples/quantized-model/candidates.jsonl \
  --output local,wandb,plot
```

To customize the list, write one JSON object per candidate, following that file. Each row selects `path`, or `url` with its `model`; rows without either reuse the command's candidate service. Paths are relative to the command's working directory. Single-model deployments supply their model IDs. Labels default to deployment directory names or model IDs; use `label` to distinguish identical names.

For one candidate, `--label` and `--method` annotate the plots. The quantization method is inferred from the deployment when omitted. Optional size coordinates produce separate comparison plots; without them, the horizontal axis uses candidate names:

| Candidate field / CLI option | Meaning |
| --- | --- |
| `weight_bits` / `--weight-bits` | Nominal weight precision |
| `bits_per_weight` / `--bits-per-weight` | Measured bits per weight, including quantization overhead |
| `model_size_gib` / `--model-size-gib` | Measured checkpoint size in GiB |

## Use existing endpoints

Supply the two service addresses and served model IDs, replacing the example values below. Both services need the same token-ID mapping and model vocabulary, and must support full-vocabulary probability output. Native vLLM uses `--max-logprobs -1`; custom Foretoken deployments set `max-logprobs: -1` in the model's `spec.engineArgs`.

```bash
foretoken eval \
  --url http://127.0.0.1:8008/v1/chat/completions --model quantized \
  --reference-url http://127.0.0.1:8009/v1/chat/completions \
  --reference-model Qwen/Qwen2.5-0.5B-Instruct \
  --output local,plot
```

The reference model ID identifies its tokenizer and model configuration. If it is a serving alias, or its files exist only on cluster nodes, use `--tokenizer-path` with a base-model repository or client-local directory containing tokenizer files and `config.json`. Foretoken deployments resolve their configured Hugging Face, ModelScope, or client-accessible local source automatically, including a separately configured tokenizer.

With a shared endpoint, omit `--reference-url`. Authentication uses `--api-key`, with `--reference-api-key` for a different reference credential.

## Compare greedy generated sequences

To see whether a candidate generates the same token IDs as a reference on identical prompts, use the maintained candidate list:

```bash
foretoken eval --reference examples/quantized-model/bf16 \
  --candidates examples/quantized-model/candidates.jsonl \
  --greedy-compare --context-length 128 --num-windows 4 --max-tokens 64 \
  --output local,wandb,plot
```

The command compares four 128-token prompts using greedy decoding (`temperature=0`), generating up to 64 tokens per prompt or stopping at EOS. Both services must use the same token-ID mapping and return `choices[0].token_ids` and `finish_reason` in their Completions responses. Each model's context must accommodate the prompt and generation budget.

`greedy_comparison_candidates.csv` reports valid and failed sample counts and the exact-sequence match rate among valid pairs. Inspect generated tokens and the first divergence in `greedy_comparison_samples.csv`.

To compare draft configurations that share a served model ID, give each candidate a distinct deployment or URL and `label`.

## Resume a distribution comparison

Keep the complete local result directory from a distribution comparison. After an interruption, repeat the original command with `--resume` pointing to that directory. Replace `results/previous-run` below with the printed path:

```bash
foretoken eval examples/quantized-model/bitsandbytes \
  --reference examples/quantized-model/bf16 \
  --resume results/previous-run --output local,plot
```

The command reuses completed work and saves results in a new directory. Keep the models, weights, tokenizer, candidates, and scoring settings unchanged.

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Compare quantization fidelity

English | [简体中文](fidelity_zh.md) · [Quality evaluation](README.md)

Compare how quantization changes a model's next-token probabilities with `foretoken eval --evaluator fidelity`. Every model receives the same token IDs from a text corpus; later prefixes use the corpus rather than generated answers. This is called teacher forcing.

## Compare a candidate

Use a reference and candidate with the same tokenizer. `--tokenizer-path` selects their shared base-model repository or local directory, including its tokenizer and `config.json`. Both services must return full-vocabulary log probabilities with token-ID keys through `/v1/completions`. For vLLM, enable this when starting each service with `--max-logprobs -1`. For a Foretoken deployment, add `max-logprobs: -1` to the model's `spec.engineArgs` and apply it with `foretoken deploy PATH`.

The example below assumes a BF16 reference at port 8009 and a bitsandbytes 4-bit candidate at port 8008, both serving `Qwen/Qwen3-0.6B`. Replace the addresses and model IDs with those of the services to compare:

```bash
foretoken eval --evaluator fidelity \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --reference-url http://127.0.0.1:8009/v1/chat/completions \
  --reference-model Qwen/Qwen3-0.6B \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --label "bitsandbytes 4-bit" --method bitsandbytes --weight-bits 4 \
  --output local,wandb
```

The summary reports KL divergence, Top-1 agreement, and centered-logit RMSE. Local results include comparison plots and per-position measurements; W&B shows them in the Fidelity section. For local results only, use `--output local`.

A Kustomize directory immediately after `eval` can replace the candidate `--url`; `--reference PATH` selects a reference deployment. A single-model deployment supplies its model ID automatically. If both models share an endpoint, omit `--reference-url` and select the reference with `--reference-model`.

## Compare methods and bit widths

Save one candidate per line in `candidates.jsonl`. Rows can reuse the command's endpoint and override `model`, or specify their own `url` or Kustomize `path`:

```jsonl
{"model":"qwen-bf16","label":"BF16","method":"BF16","weight_bits":16}
{"model":"qwen-bnb4","label":"bitsandbytes 4-bit","method":"bitsandbytes","weight_bits":4}
```

Use the IDs actually advertised by the service. Add `--candidates candidates.jsonl` to the comparison command, replacing the single-candidate `--label`, `--method`, and `--weight-bits` options. Each label must be distinct.

Plots use method colors and candidate labels. Available size coordinates produce separate plots:

| Candidate field | Meaning |
| --- | --- |
| `weight_bits` | Nominal weight precision, such as 4 or 16 bits |
| `bits_per_weight` | Measured effective bits per weight, including quantization overhead |
| `model_size_gib` | Measured checkpoint size in GiB |

Supply measured values only when available. Without size coordinates, the comparison uses candidate names on the horizontal axis. For a single candidate, the corresponding flags are `--weight-bits`, `--bits-per-weight`, and `--model-size-gib`.

## Select text and scoring positions

The default corpus is [WikiText-2](https://huggingface.co/datasets/Salesforce/wikitext), configuration `wikitext-2-raw-v1`, test split. The evaluator takes four non-overlapping 512-token windows and scores the last 16 positions of each: 64 paired positions per candidate.

Use `--dataset corpus.txt` for local text or `--dataset corpus.jsonl` for records containing a `text` field. `--text-column` selects another field. Hugging Face datasets also accept `--dataset-config` and `--split`.

Change the sample size with `--context-length`, `--num-windows`, and `--score-tokens`. The tokenizer's beginning-of-sequence token is added to each window when defined. Reference probabilities are collected once and reused for all candidates in the run.

## Read the comparisons

| Metric | Interpretation |
| --- | --- |
| Mean, median, p99 KL | `KL(reference || candidate)` over the complete vocabulary, in nats; lower is closer |
| Top-1 agreement | Fraction of positions with the same most-probable token |
| Top-k overlap | Shared top-k tokens divided by k; defaults to k=5 and 10, configurable with `--top-k` |
| Reference-token probability change | Candidate minus reference probability for the corpus token; mean shows direction, RMS shows magnitude |
| Centered-logit RMSE | Root-mean-square difference after subtracting each vector's mean log probability; invariant to a common logit offset |
| Total variation | Half the sum of absolute probability differences |

The bit-width and size plots compare mean KL, p99 KL, centered-logit RMSE, and Top-1 agreement. Position curves show where KL and logit differences increase across the scored text. Use [task evaluation](README.md) to measure answer quality and [performance evaluation](../perf/README.md) to measure serving speed.

The example below compares Qwen3-0.6B BF16 and bitsandbytes 4-bit through existing endpoints, using two 96-token WikiText-2 windows and scoring the last 32 positions of each.

![Quantization methods and nominal weight bits compared by KL, logit RMSE, and Top-1 agreement](../imgs/fidelity-weight-bits.png)

![KL and centered-logit RMSE across the 64 scored positions](../imgs/fidelity-positions.png)

The result directory contains `fidelity_candidates.csv` for candidate summaries, `fidelity_tokens.jsonl` for individual positions, and PNG plots. `metrics.json` records the corpus selection, scoring settings, and completed candidates. Full probability vectors are temporary and removed after the comparison.

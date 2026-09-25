<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Evaluate model quality

English | [简体中文](README_zh.md) · [Evaluation and profiling](../../README.md)

Score answers from a running model with lm-evaluation-harness or EvalScope. Complete the [setup](../../README.md#get-started), then choose a framework below. Add `--reference` for [reference/candidate distribution comparisons](distribution-comparison.md), including KL, bit-width plots, and logit differences.

## lm-evaluation-harness

Run 100 GSM8K math problems:

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval \
  --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 \
  --output local,wandb
```

The summary lists task scores, answer filters, sample counts, and standard errors when available. `lm-eval` is the default evaluator. Task names and parameters follow the [upstream CLI syntax](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/interface.md):

- `--num_fewshot 0` uses zero-shot prompts.
- `--log_samples` saves individual inputs and answers.
- `--model_args num_concurrent=4` selects four concurrent API requests.

Foretoken selects Chat Completions for generated answers and Completions for likelihood requests from the task definition.

### Candidate likelihood and perplexity

Select a task that scores candidate answers by their token probabilities, or measures perplexity on a text corpus. Use a [source-built Foretoken platform](../../../docs/custom-deployment.md), or an existing service that returns prompt-token log probabilities through Completions with `echo=true`:

```bash
foretoken eval examples/quickstart \
  --tasks piqa --limit 100 --output local

foretoken eval examples/quickstart \
  --tasks wikitext --limit 100 --output local
```

The tokenizer is inferred from the deployment, or from `--model` for an existing URL; override it with `--model_args tokenizer=MODEL_OR_LOCAL_DIRECTORY` when the served name is an alias or its files are only available on the server.

Candidate likelihood uses raw text by default; add `--apply_chat_template` when its evaluation protocol calls for an instruction-model template. Generation tasks retain chat messages, while perplexity always scores the original corpus, including when these tasks run together.

## EvalScope

```bash
foretoken eval examples/quickstart \
  --evaluator evalscope \
  --model Qwen/Qwen3-0.6B \
  --datasets gsm8k --limit 100 \
  --output local,wandb
```

The summary reports task scores and how many samples were scored. Category and subset scores remain in the saved reports and W&B. Use [EvalScope's native options](https://evalscope.readthedocs.io/en/latest/get_started/basic_usage.html), including `--dataset-args` and `--generation-config`, to configure the task.

For both frameworks, omit `--limit` to run the complete selected task. The evaluator and task define prompting and scoring. Run `foretoken eval --evaluator lm-eval --help` or `foretoken eval --evaluator evalscope --help` for the corresponding options.

## Use an existing endpoint

Replace the deployment directory with the service's Chat Completions URL and model name:

```bash
foretoken eval \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --evaluator lm-eval \
  --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 \
  --output local,wandb
```

This mode uses no Kubernetes resources. Add `--api-key` when authentication is required. For a Foretoken Gateway deployment, pass its Kustomize directory so the command discovers the address and routing headers.

## Resume an evaluation

Keep local output to retain progress. After an interruption, repeat the original command with `--resume` pointing to its printed result directory. Replace `results/previous-run` below with that directory:

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval --tasks gsm8k --limit 100 \
  --resume results/previous-run --output local
```

The resumed invocation writes a new result directory, reuses completed work, and reports the combined scores. The source directory remains unchanged; if interrupted again, resume from the newest directory. Keep model weights, task configuration, generation settings, and sample selection unchanged.

| Evaluation | Reused work |
| --- | --- |
| lm-evaluation-harness | Completed generations for text-only tasks, including repeated sampling; only missing generations are requested |
| EvalScope | Completed predictions and reviews for independent samples, with the same service URL and evaluation settings |
| Distribution comparison | Complete scoring windows; see [resuming a comparison](distribution-comparison.md#resume-a-comparison) |

Use `--resume` instead of native `--use_cache` or `--use-cache` for this workflow. Likelihood and perplexity tasks, performance tests, trace replay, parameter sweeps, and SLO searches do not support this option.

## Read scores

Open the result directory printed by the command:

| File or directory | Contents |
| --- | --- |
| `metrics.json` | Task scores, subsets, answer filters, sample counts, and available uncertainty or execution status |
| `native/` | The framework's reports and any generated sample records |
| `evaluator.log` | The evaluator's execution log |

W&B provides task metrics, a score table, and native reports as a downloadable artifact. Common [output settings](../../README.md#read-and-save-results) select destinations and organize comparisons.

Compare scores using the same evaluator, task configuration, and sample selection.

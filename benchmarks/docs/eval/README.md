<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Evaluate model quality

English | [简体中文](README_zh.md) · [Evaluation and profiling](../../README.md)

Score answers from a running model with lm-evaluation-harness or EvalScope. Complete the [setup](../../README.md#get-started), then choose a framework below. To compare quantization methods using full-vocabulary KL, bit-width plots, and logit differences, see [quantization fidelity](fidelity.md).

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

Connection settings come from Foretoken. The Chat Completions interface supports generation-based tasks; select those rather than tasks requiring candidate-answer log-likelihoods.

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

## Read scores

Open the result directory printed by the command:

| File or directory | Contents |
| --- | --- |
| `metrics.json` | Task scores, subsets, answer filters, sample counts, and available uncertainty or execution status |
| `native/` | The framework's reports and any generated sample records |
| `evaluator.log` | The evaluator's execution log |

W&B provides task metrics, a score table, and the saved evaluation files as a downloadable artifact. Common [output settings](../../README.md#read-and-save-results) select destinations and organize comparisons.

Compare scores using the same evaluator, task configuration, and sample selection.

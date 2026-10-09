<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Evaluate model quality

English | [简体中文](README_zh.md) · [Evaluation and profiling](../../README.md)

Use `foretoken eval` to score generated text and existing videos. Complete the [setup](../../README.md#get-started), then choose an evaluation method below. For text models, add `--reference` for [reference/candidate comparisons](distribution-comparison.md): teacher-forced probabilities or greedy generated token sequences.

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

### Candidate likelihood and perplexity

PIQA selects answers by comparing their probabilities. WikiText measures perplexity: lower values mean the model predicts the text more readily. These tasks need a [source-built Foretoken platform](../../../docs/custom-deployment.md) or an existing Completions service that returns input-token log probabilities:

```bash
foretoken eval examples/quickstart \
  --tasks piqa --limit 100 --output local

foretoken eval examples/quickstart \
  --tasks wikitext --limit 100 --output local
```

The tokenizer is inferred from the deployment, or from `--model` for an existing URL; override it with `--model_args tokenizer=MODEL_OR_LOCAL_DIRECTORY` when the served name is an alias or its files are only available on the server.

Candidate scoring uses raw text by default. Add `--apply_chat_template` when the task requires an instruction-model template. Perplexity uses the original corpus without a chat template.

## Compare task scores across deployments

After preparing the [quantized-model examples](../../../examples/quantized-model/README.md), pass their Kustomize directories before the task options to score the same task on each service:

```bash
foretoken eval examples/quantized-model/bf16 examples/quantized-model/bitsandbytes \
  --tasks piqa --limit 100 --output local,wandb,plot
```

The deployments are evaluated in turn. Compare task scores and available standard errors in `evaluation_comparison.csv`; detailed evaluator reports are saved with each run.

For [reference/candidate distribution or greedy sequence comparisons](distribution-comparison.md), provide `--reference` explicitly with multiple candidate deployment paths.

## EvalScope

```bash
foretoken eval examples/quickstart \
  --evaluator evalscope \
  --model Qwen/Qwen3-0.6B \
  --datasets gsm8k --limit 100 \
  --output local,wandb
```

The summary reports task scores and sample counts; saved reports provide category and subset scores. Use [EvalScope's native options](https://evalscope.readthedocs.io/en/latest/get_started/basic_usage.html), including `--dataset-args` and `--generation-config`, to configure the task.

For both frameworks, omit `--limit` to run the complete selected task. The evaluator and task define prompting and scoring. Run `foretoken eval --evaluator lm-eval --help` or `foretoken eval --evaluator evalscope --help` for the corresponding options.

## Evaluate video quality

Prepare VBench, then score the existing MP4 or GIF files in `VIDEO_DIR`:

```bash
foretoken eval setup vbench
foretoken eval --video VIDEO_DIR --output local
```

`setup` saves reusable settings in `foretoken-evaluators.yaml`. `eval --video` scores videos without generating them; use `foretoken perf video` for generation and serving-performance measurements.

For Foretoken video results, prompts come from `raw_results.json`; otherwise they come from filenames. Use `--prompt-file` for a JSON mapping such as `{"clip.mp4": "A red car drives past."}` and `--dimension` to select dimensions. Scores are saved in `metrics.json`, with run provenance in `config.json`.

### Use your own image or configuration

Pass `--image IMAGE` to `setup` for a different VBench image. If its checkpoints are already prepared, you can instead provide an evaluator YAML with `--config PATH`:

```yaml
evaluators:
  vbench:
    image: foretoken-vbench:local
    cache: .foretoken/evaluators/vbench/cache
```

For direct evaluation, the image and cache must exist locally. The image must contain VBench and an `org.foretoken.vbench.commit` label; relative cache paths resolve beside the YAML. The published image uses NVIDIA GPUs; on a shared host, select an available GPU with `CUDA_VISIBLE_DEVICES`.

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

## Resume text evaluation or model comparison

For a single-deployment evaluation, keep local output to retain progress. After an interruption, repeat the original command with `--resume` pointing to its printed result directory. Replace `results/previous-run` below with that directory:

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval --tasks gsm8k --limit 100 \
  --resume results/previous-run --output local
```

The resumed invocation writes a new result directory, reuses completed work, and reports the combined scores. The source directory remains unchanged; if interrupted again, resume from the newest directory. Keep model weights, task configuration, generation settings, and sample selection unchanged.

| Evaluation | Reused work |
| --- | --- |
| lm-evaluation-harness | Completed text generations, including repeated sampling, and completed likelihood-scoring windows for candidate answers and perplexity |
| EvalScope | Completed predictions and reviews for independent samples, with the same service URL and evaluation settings |
| Distribution comparison | Complete scoring windows; see [resuming a distribution comparison](distribution-comparison.md#resume-a-distribution-comparison) |

Use `--resume` instead of native `--use_cache` or `--use-cache` for this workflow.

## Read scores

Open the result directory printed by the command:

| File or directory | Contents |
| --- | --- |
| `metrics.json` | Task scores, subsets, answer filters, sample counts, and available uncertainty or execution status |
| `native/` | The framework's reports and any generated sample records |
| `evaluator.log` | The evaluator's execution log |

See [output settings](../../README.md#read-and-save-results) to choose where results are saved.

Compare scores using the same evaluator, inputs, prompts, and scoring settings.

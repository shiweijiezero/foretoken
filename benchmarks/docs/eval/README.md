<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Evaluate model quality

English | [简体中文](README_zh.md) · [Evaluation and profiling](../../README.md)

Use `foretoken eval` to score generated text and existing videos. Complete the [setup](../../README.md#get-started), then choose an evaluation method below. Add `--reference` for [reference/candidate distribution comparisons](distribution-comparison.md), including KL, bit-width plots, and logit differences.

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

## Evaluate video quality

On a Linux x86_64 machine with Docker, an NVIDIA driver supporting CUDA 12.1, and the NVIDIA Container Toolkit, prepare a VBench image and its checkpoints before evaluating existing MP4 or GIF videos. The image must contain VBench and carry an `org.foretoken.vbench.commit` label. From a Foretoken source checkout, build the provided image once:

```bash
docker build -f benchmarks/docker/vbench/Dockerfile -t foretoken-vbench:local .
foretoken eval setup vbench --image foretoken-vbench:local
foretoken eval --video results/video-run \
  --evaluator vbench \
  --output local,wandb
```

The image fixes the VBench source and its Python, PyTorch, and CUDA dependencies without changing Foretoken's environment. Setup checks the image and prepares a checkpoint cache under `.foretoken/evaluators/vbench` beside the YAML. It downloads weights for all 10 supported video-quality dimensions but does not run GPU evaluation. Image building and checkpoint downloads need network access and several GB of disk space; upstream checkpoints come from Hugging Face and other hosts.

After preparation succeeds, setup writes `foretoken-evaluators.yaml` with the exact image digest (or a local image ID) and cache path. Evaluation reuses it without installing dependencies. Both commands search upward for the nearest YAML; if none exists, setup creates it in the current directory. Use `--config PATH` for another YAML and setup's `--directory PATH` to change the default checkpoint location. A failed setup does not publish new YAML; inspect `setup.log` in the managed directory and retry. Once a VBench image is published in a registry, pass that image reference to `--image` instead of building locally.

`foretoken perf video` generates videos and measures serving performance; `foretoken eval --video` only scores existing videos and does not regenerate them. When the video directory is a Foretoken `perf video` result, the command derives exact prompts from `raw_results.json`. For another video directory, VBench infers prompts from file names, or you can pass a VBench JSON mapping with `--prompt-file`. Select a subset with `--dimension NAME [NAME ...]`; by default all 10 video-quality dimensions run.

The new evaluation result's `config.json` records the selected image, its local ID and registry digest when available, the VBench commit from the image label, selected dimensions, prompt source, and number of videos. Foretoken video generation saves its own `config.json` and `raw_results.json` in the source directory.

### Use an existing image

If you already have a VBench image and prepared checkpoints, you can create the YAML yourself:

```yaml
evaluators:
  vbench:
    image: foretoken-vbench:local
    cache: .foretoken/evaluators/vbench/cache
```

Then run `foretoken eval --video VIDEO_DIR` directly. If checkpoints are missing, run `foretoken eval setup vbench` to prepare them and pin the image in the YAML. Other evaluators' settings are retained.

The cache directory must exist before evaluation. Relative YAML paths resolve beside the file. Docker mounts the video directory read-only and writes VBench reports to Foretoken's result directory. The supplied image must already be available locally for direct evaluation; setup pulls it when needed. The current image targets NVIDIA GPUs; MetaX evaluation needs its own validated image and is not supported yet.

On a shared NVIDIA host, set `CUDA_VISIBLE_DEVICES` to an available host GPU index before running `foretoken eval --video`; the VBench container will use that GPU.

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
| lm-evaluation-harness | Completed text generations, including repeated sampling, and completed likelihood-scoring windows for candidate answers and perplexity |
| EvalScope | Completed predictions and reviews for independent samples, with the same service URL and evaluation settings |
| Distribution comparison | Complete scoring windows; see [resuming a comparison](distribution-comparison.md#resume-a-comparison) |

Use `--resume` instead of native `--use_cache` or `--use-cache` for this workflow.

## Read scores

Open the result directory printed by the command:

| File or directory | Contents |
| --- | --- |
| `metrics.json` | Task scores, subsets, answer filters, sample counts, and available uncertainty or execution status |
| `native/` | The framework's reports and any generated sample records |
| `evaluator.log` | The evaluator's execution log |

W&B provides task metrics, a score table, and native reports as a downloadable artifact. Common [output settings](../../README.md#read-and-save-results) select destinations and organize comparisons.

Compare scores using the same evaluator, task configuration, and sample selection.

# Model Service Evaluation

English | [简体中文](README_zh.md)

Use `foretoken perf` to measure latency and throughput, and `foretoken eval` to score model answers with lm-evaluation-harness or EvalScope.

## Get started

Install the performance and quality evaluation tools with Python 3.11 or later:

```bash
pip install 'foretoken[bench,eval]'

# From a source checkout:
# pip install -e '.[bench,eval]'
```

The commands below run from the repository checkout prepared by the [Quick Start](../README.md#quick-start). Passing a Kustomize directory reuses its running services or deploys them when absent. Only resources created by the evaluation command are removed afterwards. A single-model deployment supplies the model name automatically; use `--model` to choose among multiple models.

## Measure performance

```bash
foretoken perf examples/quickstart \
  --prompt "Explain what a token is in one sentence." \
  --max-concurrency 4 --num-prompts 20 --max-tokens 128 \
  --output local
```

The summary reports request success, latency, and throughput. Streamed requests also report time to first token (TTFT) and time per output token (TPOT). See [Performance metrics](metrics.md) for definitions and units.

For conversation datasets, later turns use recorded answers as history by default; [Local conversations](docs/coomon_commands/conversations.md) explains how to select generated history and limit turns.

[Performance examples](docs/examples.md) cover datasets, multi-turn conversations, arrival rates, trace replay, parameter sweeps, SLO searches, and video generation. Add `--profile` to capture a workload as described in [Profiling](../observability/profiling.md). All performance options are listed by `foretoken perf --help`.

## Evaluate model quality

### lm-evaluation-harness

Run a small GSM8K math evaluation:

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval \
  --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 \
  --output local
```

`lm-eval` is the default evaluator. Task names, sampling settings, few-shot counts, and other evaluation options use the [upstream CLI syntax](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/interface.md). For example, add `--num_fewshot 0` for zero-shot evaluation or `--log_samples` to save individual inputs and answers. Connection settings come from Foretoken; other API options remain available through `--model_args`, such as `--model_args num_concurrent=4`.

The Chat Completions connection runs generation-based tasks. Tasks that score candidate answers using log-likelihood require a different model interface; select a generation-based task variant for this endpoint.

### EvalScope

```bash
foretoken eval examples/quickstart \
  --evaluator evalscope \
  --model Qwen/Qwen3-0.6B \
  --datasets gsm8k --limit 100 \
  --output local
```

Use [EvalScope's native options](https://evalscope.readthedocs.io/en/latest/get_started/basic_usage.html), including `--dataset-args` and `--generation-config`, to configure the evaluation. For both evaluators, omit `--limit` to run the complete selected task. Prompt and scoring settings are defined by the chosen evaluator and task.

## Use an existing endpoint

Replace the deployment directory with `--url` and provide the model name exposed by the service. This mode uses no Kubernetes resources:

```bash
foretoken eval \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --evaluator lm-eval \
  --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 \
  --output local
```

Use the service's actual Chat Completions URL and pass `--api-key` when authentication is required. `foretoken perf` accepts the same URL and model options. For a Foretoken Gateway deployment, pass its Kustomize directory so the command discovers the address and routing headers.

## Read and save results

The terminal prints the selected model and evaluator followed by task scores, sample counts, and standard errors when available. Detailed subset scores and answer filters remain available in the saved results and W&B score table. Compare runs using the same evaluator, task configuration, and sample selection.

Both commands default to console output, local files, and W&B. Authenticate with `wandb login` before using W&B, or select `--output local` as in the examples above.

| Output selection | Result |
| --- | --- |
| Omit `--output` or use `local,wandb` | Print results, save local files, and upload to W&B |
| `local` | Print results and save local files |
| `wandb` | Print results and upload to W&B |
| `local,quiet` | Save local files without console progress or summaries |
| `local,wandb,quiet` | Save and upload results without console progress or summaries |

Local results are stored in separate directories under `results/`; `--output-dir` changes the parent. The command prints the saved location. Quality runs save task metrics in `metrics.json`, the evaluator's reports and any sample records in `native/`, and execution logs in `evaluator.log`. Performance result files are described in [Performance metrics](metrics.md).

For quality evaluations, W&B provides task metrics, a score table, and the saved evaluation files as an artifact. Use `--wandb-project`, `--wandb-entity`, `--wandb-group`, and `--wandb-run-name` to organize comparisons. [W&B performance output](docs/coomon_commands/wandb.md) describes the latency, throughput, and request-level views for `perf`.

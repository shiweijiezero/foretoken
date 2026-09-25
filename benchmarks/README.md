# Evaluation and profiling

English | [简体中文](README_zh.md)

Measure service latency and throughput with `foretoken perf`, score model answers with `foretoken eval`, and inspect execution bottlenecks with profiling.

## Get started

Install Foretoken with Python 3.11 or later:

```bash
pip install foretoken

# From a source checkout:
# pip install -e .
```

Run the examples from the repository checkout prepared by the [Quick Start](../README.md#quick-start). They save results locally and to W&B; run `wandb login` once before using W&B.

Pass a Kustomize directory to use its model service. A single-model deployment supplies the model name automatically; use `--model` to choose among multiple models. To measure an existing endpoint, replace the directory with `--url` and provide its model name.

## Measure performance

```bash
foretoken perf examples/quickstart \
  --prompt "Explain what a token is in one sentence." \
  --max-concurrency 4 --num-prompts 20 --max-tokens 128 \
  --output local,wandb
```

The summary reports request success, latency, and throughput. Streamed requests also report time to first token (TTFT) and time per output token (TPOT).

[Performance examples](docs/perf/README.md) cover datasets, conversations, arrival rates, trace replay, parameter sweeps, SLO searches, and video generation. Definitions and units are in [Performance metrics](metrics.md).

## Evaluate model quality

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 --output local,wandb
```

This scores 100 GSM8K math problems and reports the task's metrics and sample counts. [Quality evaluation](docs/eval/README.md) covers lm-evaluation-harness and EvalScope. Add `--reference` for [reference/candidate probability comparisons](docs/eval/distribution-comparison.md).

## Profile execution

Capture CPU/GPU execution while a workload runs, then open the timeline with `foretoken profile view`. The [profiling guide](docs/profile/README.md) covers setup, capture, and viewing with PyTorch Profiler, NVIDIA Nsight Systems, and MetaX mcTracer.

## Read and save results

`perf` and `eval` default to console output, local files, and W&B. Select destinations with `--output`:

| Output selection | Result |
| --- | --- |
| Omit `--output` or use `local,wandb` | Print results, save local files, and upload to W&B |
| `local` | Print results and save local files |
| `wandb` | Print results and upload to W&B |
| `local,quiet` | Save local files without console summaries |
| `local,wandb,quiet` | Save and upload results without console summaries |

`quiet` saves preparation and execution logs in `run.log` instead of printing progress; errors remain visible. With W&B selected, this log is also uploaded as an artifact.

Local results use a separate directory under `results/` for each run; `--output-dir` changes the parent. Use `--wandb-project`, `--wandb-entity`, `--wandb-group`, and `--wandb-run-name` to organize runs.

See [performance results](docs/perf/wandb.md) for latency and throughput charts, [quality results](docs/eval/README.md#read-scores) for task scores and native reports, and [profile viewing](docs/profile/README.md#inspect-results) for retained execution captures.

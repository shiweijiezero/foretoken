# Evaluation and profiling

English | [简体中文](README_zh.md)

Measure service latency and throughput with `foretoken perf`, score model answers with `foretoken eval`, and inspect execution bottlenecks with profiling.

[Experiment commands](docs/recipes.md)

## Get started

Install Foretoken with Python 3.11 or later:

```bash
pip install foretoken

# From a source checkout:
# pip install -e .
```

Run the examples from the repository checkout prepared by the [Quick Start](../README.md#quick-start). Run `wandb login` before first using W&B. Random workloads reuse local model/cache tokenizers when available; set `FORETOKEN_HF_ENDPOINT` to choose a Hugging Face endpoint explicitly.

Pass a Kustomize directory to use its model service. A single-model deployment supplies the model name automatically; use `--model` to choose among multiple models. To measure an existing endpoint, replace the directory with `--url` and provide its model name.

## Measure performance

```bash
foretoken perf examples/quickstart \
  --prompt "Explain what a token is in one sentence." \
  --max-concurrency 4 --num-prompts 20 --max-tokens 128 \
  --output local,wandb
```

The summary reports request success, latency, and throughput. Streamed requests also report time to first token (TTFT) and time per output token (TPOT).

Performance time values may include a unit such as `s`, `min`, or `h`; unitless values use seconds.

[Performance examples](docs/perf/README.md) cover datasets, conversations, arrival rates, trace replay, parameter sweeps, SLO searches, and video generation. Definitions and units are in [Performance metrics](docs/metrics.md).

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
| `wandb` | Print results, retain local files, and publish to W&B in the background |
| `plot` | Retain results and export PDF, SVG, PNG, and CSV |
| `experiment` | Organize the run under an experiment iteration and capture its source and command context |
| `raw` | Also retain actual request and response content for HTTP performance runs, with local results |
| `local,wandb,plot` | Save results, export figures, and upload to W&B |
| `local,quiet` | Save local files without console summaries |
| `local,wandb,quiet` | Save and upload results without console summaries |

Local results use a separate directory under `results/` for each run; `--output-dir` changes the parent. With W&B selected, open the printed run link to follow progress and view results. Use `--wandb-project`, `--wandb-entity`, `--wandb-group`, and `--wandb-run-name` to organize runs. See [W&B output](docs/perf/wandb.md) to view results or resume an upload.

| Record | Use |
| --- | --- |
| `metrics.json` | Read the measured aggregate results |
| `context.json` | Check the run outcome and phase separately from the measurement exit status |
| `run.log` | Inspect preparation, execution, and errors |
| `environment.json`, `sources/` | Inspect the service configuration and available source archives, saved locally |
| `raw_output.jsonl` (HTTP), `raw_results.jsonl` (video) | Inspect requests as they complete, including records retained after interruption |
| `responses.jsonl`, with `--output local,raw` | Inspect HTTP inputs and response content, including reasoning and tool calls |

Logs are recorded in both normal and quiet modes. `quiet` suppresses benchmark progress and summaries; errors and W&B synchronization messages remain visible. Warmup records use the `warmup_` filename prefix. Quality scores, framework reports, and profiling captures are described in the linked guides below.

Use `experiment` to group measurements under a named iteration. Run from the checkout containing the code changes:

```bash
foretoken perf examples/quickstart --num-prompts 20 \
  --output experiment --output-dir results/reduce-ttft/queue-aware-routing \
  --iteration baseline
```

`perf`, `eval`, and `perf video` share these recording options. Commands with the same directory and iteration name add separate runs; omitting `--iteration` creates a new numbered iteration per command. `experiment` includes local storage and can be combined with `wandb`, `plot`, and `quiet`. See the [experiment records guide](../rsi-playbooks/experiment-records.md) for the layout, source records, and author-written notes.

See [performance results](docs/perf/wandb.md) for latency and throughput charts, [quality results](docs/eval/README.md#read-scores) for task scores and native reports, and [profile viewing](docs/profile/README.md#inspect-results) for retained execution captures.

Use `foretoken plot RESULT_DIR [RESULT_DIR ...]` to redraw or compare saved results. See [plotting examples](docs/recipes.md#plot-saved-results) for metric selection, layout and output locations.

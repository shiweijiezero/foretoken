# HTTP Performance Benchmarks

English | [简体中文](README_zh.md)

Use `foretoken bench` to measure latency and throughput against a Foretoken deployment or an existing OpenAI-compatible endpoint.

## Before you start

Run benchmark commands from the repository root with Python 3.10 or later:

```bash
pip install 'foretoken[bench]'

# For source installation from the repository:
# pip install -e .
# pip install -e '.[bench]'
```

For a Foretoken deployment, install the platform before benchmarking a Kustomize configuration:

```bash
foretoken install
foretoken bench examples/quickstart
```

The command reuses the Quick Start when it is already running. Otherwise it deploys the rendered resources and removes only the resources it created after the benchmark.

To benchmark an existing endpoint, Foretoken and its Kubernetes platform are not required:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --prompt "Hello" \
  --parallel 2 \
  --number 20
```

## Results and output

Without `--output`, the benchmark prints a summary, writes local artifacts under `results/`, and attempts a W&B upload. If W&B is unavailable, local results remain available.

Standard request loads use EvalScope for load scheduling, HTTP execution, and latency/token metrics while preserving complete local or Hub request bodies. Multi-turn mode is enabled only by `--max-turns`; use `--max-turns -1` for the complete dataset conversation. Each dataset row then becomes an interactive conversation, and the model's actual answer is appended before the next user turn is sent. The local directory contains Foretoken's `config.json` and `metrics.json` together with EvalScope's `benchmark_args.json`, `benchmark_summary.json`, `benchmark_percentile.json`, `benchmark_data.db`, and `benchmark.log`; multi-turn runs also contain `trace_summary.json`, `workload_throughput.json`, and `workload_timeline.json`. Trace replay writes `raw_output.json` because it additionally records replay-delay fields.

Standard loads store per-request records in `benchmark_data.db` and failure details in `benchmark.log`; `raw_output.json` is reserved for trace replay and combined multi-dataset records. `metrics.json` keeps the Foretoken summary consumed by parameter sweeps and W&B. Consumers of earlier results should migrate `mode: run_benchmark` to `standard_load`, `mode: sweep` to `parameter_sweep`, and multi-dataset `dataset_numbers` to `dataset_request_counts`. Multi-turn adds `multi_turn: true` and a `conversation` object while retaining `request_num`, `success_num`, latency, and throughput as turn-request metrics. For multiple multi-turn datasets, combined turn metrics and attempted-conversation throughput are exact; conversation percentile distributions remain under `conversation.per_dataset` because EvalScope 1.11.1 does not persist conversation IDs in its SQLite request rows.

`--output` replaces the default output choices:

| Goal | `--output` value |
| --- | --- |
| Default console, local artifacts, and W&B | omit `--output` |
| Local artifacts only | `local` |
| Local artifacts without console output | `local,quiet` |
| Local artifacts and W&B without console output | `local,wandb,quiet` |
| W&B only | `wandb` |

To suppress console output while retaining results, combine `quiet` with `local`, `wandb`, or both. Use `--output-dir PATH` to change the local artifact directory.

## Metrics

The summary includes request latency, time to first token (TTFT), time per output token (TPOT), failure rate, input/output token counts, and output throughput. In multi-turn mode, these request fields count HTTP turns. `number` is the configured conversation count, `parallel` is concurrent conversations, and `conversation` contains attempted-conversation throughput plus EvalScope's conversation latency, first-turn TTFT, time to the first token of the final answer, decode throughput, and cache metrics. A failed turn stops that conversation; turn success must not be read as conversation success.

For parameter sweeps, `token/s/user` means output throughput divided by the configured closed-loop `--parallel` value. It is not a count of real users or active sessions. For multi-turn sweeps, the same denominator is explicitly a concurrent conversation and the console and Pareto plot use that label. In open-loop runs (`--rate`), its denominator is one, so it equals total output throughput. `token/s/GPU` divides output throughput by the configured GPU count for that point.

A sweep always writes every valid point. It creates `pareto/PARETO.png` only when the sweep has at least two valid points.

## Next steps

Scenario recipes for datasets, random prompts, trace replay, prefix reuse, multiple datasets, and parameter sweeps are in [Benchmark examples](docs/examples.md). The command reference and result formats are exposed through `foretoken bench --help` and the generated local artifacts.

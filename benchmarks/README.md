# Benchmarks

English | [简体中文](README_zh.md)

Use `foretoken bench` to measure latency and throughput against a Foretoken deployment or an existing OpenAI-compatible endpoint.

## Before you start

Run benchmark commands from the repository root with Python 3.10 or later:

```bash
pip install -e '.[bench]'
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

`--output` replaces the default destination set:

| Goal | `--output` value |
| --- | --- |
| Default console, local artifacts, and W&B | omit `--output` |
| Local artifacts only | `local` |
| Local artifacts without console output | `local,quiet` |
| Local artifacts and W&B without console output | `local,wandb,quiet` |
| W&B only | `wandb` |

To suppress console output while retaining results, combine `quiet` with `local`, `wandb`, or both. Use `--output-dir PATH` to change the local artifact directory.

## Metrics

The summary includes request latency, time to first token (TTFT), time per output token (TPOT), failure rate, input/output token counts, and output throughput.

For parameter sweeps, `token/s/user` means output throughput divided by the configured closed-loop `--parallel` value. It is not a count of real users or active sessions. In open-loop runs (`--rate`), its denominator is one, so it equals total output throughput. `token/s/GPU` divides output throughput by the configured GPU count for that point.

A sweep always writes every valid point. It creates `pareto/PARETO.png` only when the sweep has at least two valid points.

## Next steps

Scenario recipes for datasets, random prompts, trace replay, prefix reuse, multiple datasets, and parameter sweeps are in [Benchmark examples](docs/examples.md). The command reference and result formats are exposed through `foretoken bench --help` and the generated local artifacts.

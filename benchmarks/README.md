# Benchmarks

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

## On-demand profiling

Profile only a few requests from a Foretoken Kubernetes deployment:

```bash
foretoken bench examples/quickstart \
  --profile \
  --number 2 \
  --output local
```

The command prepares its normal workload, then submits one capture window to the selected service's model-server Pods immediately before request dispatch. The runtime waits `--profile-delay` seconds (default `0`), captures for up to `--profile-duration` seconds (default `5`), and stops and exports without depending on the workstation connection. No additional warm-up request is sent. Requests continue during the delay; if the benchmark finishes first, the command cancels the remaining window. A workload that ends during the delay produces a status report but no trace. Native profiler startup also takes time, so a very short workload may finish before any inference is captured.

Profiling control uses Kubernetes exec and the existing Pod-local management listener: it creates no profiling port-forward, Service, or YAML setting. Your Kubernetes identity needs Pod exec access. Use matching current-source CLI and model-server images; the source image build includes the required vLLM profiling backport. Older images only providing immediate start/stop are incompatible with window submission. Ordinary benchmark frontend access is unchanged.

Results are copied to `results/profiles/<capture-id>/<pod>/`, including `capture.json` and native trace files. Export time is additional to the capture duration. Only successfully copied artifacts are removed from the Pod. If stop, export, or retrieval cannot be confirmed, the command reports an error and retains any benchmark-created deployment for recovery. Open `.pt.trace.json.gz` in [Perfetto](https://ui.perfetto.dev/).

Profiling slows inference and can produce large files, so keep the workload small. `--profile` requires a Kustomize deployment path and cannot be combined with `--url` or `--bench-params`. One capture may use a model-server at a time. This path currently implements one Torch window; repeated windows, a standalone profiling command, Nsight, and MetaX validation remain follow-up work.

## Results and output

Without `--output`, the benchmark prints a summary, writes local artifacts under `results/`, and attempts a W&B upload. If W&B is unavailable, local results remain available.

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

The summary includes request latency, time to first token (TTFT), time per output token (TPOT), failure rate, input/output token counts, and output throughput.

For parameter sweeps, `token/s/user` means output throughput divided by the configured closed-loop `--parallel` value. It is not a count of real users or active sessions. In open-loop runs (`--rate`), its denominator is one, so it equals total output throughput. `token/s/GPU` divides output throughput by the configured GPU count for that point.

A sweep always writes every valid point. It creates `pareto/PARETO.png` only when the sweep has at least two valid points.

## Next steps

Scenario recipes for datasets, random prompts, trace replay, prefix reuse, multiple datasets, and parameter sweeps are in [Benchmark examples](docs/examples.md). The command reference and result formats are exposed through `foretoken bench --help` and the generated local artifacts.

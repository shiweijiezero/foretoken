# Benchmark

English | [简体中文](README_zh.md)

Foretoken Benchmark measures request latency, TTFT, TPOT, and throughput through an OpenAI-compatible API. Use the same CLI either to benchmark an existing service or to let Kubernetes temporarily deploy a Foretoken workload and benchmark it from inside the cluster.

## Install

From the source checkout:

```bash
python -m pip install --upgrade pip
python -m pip install ./benchmarks
```

The commands are:

```text
foretoken bench run (--base-url URL | --deploy PATH) [OPTIONS]
foretoken bench cleanup RUN_ID
```

## Benchmark an existing service

Point the CLI at an OpenAI-compatible `/v1` API and select its model:

```bash
foretoken bench run \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-0.6B
```

By default this sends `Hello` 100 times with one request in flight. Change the load and prompt when needed:

```bash
foretoken bench run \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-0.6B \
  --prompt "Explain prefix caching" \
  --num-requests 200 \
  --max-concurrency 16
```

For an authenticated endpoint, use the environment rather than a command-line flag. The API key is not written to result artifacts:

```bash
OPENAI_API_KEY=... foretoken bench run \
  --base-url https://inference.example.com/v1 \
  --model Qwen/Qwen3-0.6B
```

## Workloads and experiment records

`--dataset` accepts a local JSONL workload, a Hugging Face dataset selector such as `org/name:split`, or `random`. Multiple local/Hugging Face sources are run sequentially and share `--num-requests`.

```bash
foretoken bench run \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-0.6B \
  --dataset workload.jsonl \
  --num-requests 200
```

Synthetic requests need a tokenizer:

```bash
foretoken bench run \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-0.6B \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 \
  --max-prompt-length 512
```

Add `--wandb` to record the run in Weights & Biases. See [examples](doc/examples.md) for local, Hugging Face, random, and multi-dataset workloads.

## Deploy, then benchmark

Use this with a Kubernetes cluster where the Foretoken control plane is already installed. `--deploy` takes one YAML file or a Kustomize directory containing one `FrontendService` and one or more `ModelService` objects. The CLI creates an isolated Namespace, PVC, and Benchmark Job, then returns the artifacts locally.

Build the runner image and make it reachable from every Kubernetes node. A registry is the normal option:

```bash
make image-benchmark
docker tag foretoken-benchmark:dev registry.example.com/foretoken/benchmark:dev
docker push registry.example.com/foretoken/benchmark:dev
export FORETOKEN_BENCHMARK_IMAGE=registry.example.com/foretoken/benchmark:dev
```

Then run the supplied workload:

```bash
foretoken bench run --deploy examples/quickstart
```

When the manifests contain one `ModelService`, its model is selected automatically. With several models, choose one explicitly:

```bash
foretoken bench run \
  --deploy path/to/workload \
  --model Qwen/Qwen3-0.6B \
  --num-requests 200 \
  --max-concurrency 16
```

The cluster needs a default `StorageClass`; otherwise select it:

```bash
foretoken bench run \
  --deploy examples/quickstart \
  --storage-class local-path
```

### Lifecycle

Before starting the Benchmark Job, the CLI waits for the selected `ModelService` and for the Frontend to load its routable serving snapshot. Successful runs copy results locally and delete the temporary Namespace. Failed runs retain resources for inspection and print the cleanup command:

```bash
foretoken bench cleanup bench-20260817-143210-a1b2c3
```

Use `--keep` to retain resources after a successful managed run. Managed mode currently accepts one local JSONL workload; run Hugging Face, random, and multi-dataset workloads directly against an existing service.

## Load controls

```text
--num-requests N       Total requests; default 100
--max-concurrency N    Maximum in-flight requests; default 1
--request-rate R       Poisson arrival rate; -1 disables pacing
--open-loop            Remove the in-flight request limit
--max-tokens N         Maximum generated tokens per request; default 128
--temperature T        Sampling temperature; default 0
--no-stream            Use non-streaming responses; TTFT/TPOT are unavailable
```

The default is closed-loop: `--max-concurrency` limits in-flight requests. `--request-rate` enables Poisson pacing; add `--open-loop` to remove the concurrency limit.

## Result files

Each run writes:

```text
results/<run-id>/
├── manifest.json       # Run and resource-cleanup state
├── config.json
├── raw-output.json
├── metrics.json
└── logs/              # Job and Kubernetes diagnostics in deploy mode
```

`manifest.json` records only the run ID, execution mode, and resource-cleanup state; `cleanup` uses it to verify Kubernetes resource ownership. Performance results are in `metrics.json` and `raw-output.json`. `raw-output.json` records per-request status, timing, token counts, and errors, but not generated response text. Multi-dataset runs also create one subdirectory per source with the same filenames.

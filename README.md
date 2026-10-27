# Foretoken

English | [简体中文](README_zh.md) | [Documentation](docs/README.md)

Foretoken is an RSI-friendly (Recursive Self-Improvement) orchestration framework for generative inference.

Built on inference engines such as vLLM and SGLang, Foretoken organizes multiple generation instances into a cluster service for request routing, autoscaling, instance management, and benchmarking.
We aim to turn an inference cluster into a token factory that continuously converts compute into tokens while meeting latency and quality requirements.
Unified deployment, evaluation, and profiling let agents change inference systems, measure the effects, and use the results to guide the next iteration in an [RSI loop](rsi-playbooks/README.md).

## When to Use Foretoken

- Serve one or more models across multiple GPUs or nodes.
- Route requests based on load, queue depth, or KV cache state.
- Autoscale inference instances based on traffic and SLO targets.
- Compare aggregated serving, Prefill/Decode disaggregation, and different parallelism strategies.
- Use the same orchestration stack across NVIDIA and MetaX accelerators.

If you only need to serve a single model on one GPU, using an inference engine such as vLLM directly is usually enough.

## Features and Status

| Feature | Description | Status |
|---|---|---|
| [RSI](#optimize-inference-with-rsi) | Agent-driven research, implementation, and evaluation for iterative inference optimization | In development |
| [Evaluation](benchmarks/README.md) | Measure service performance and model quality | In development |
| [Profiling](benchmarks/docs/profile/README.md) | Capture PyTorch, NVIDIA Nsight Systems, or MetaX mcTracer timelines for a model service | In development |
| Hardware support | Common interfaces for device capabilities, runtimes, communication, and metrics; see [MetaX deployment](docs/deployment/metax.md) | In development |
| Request routing | Select instances based on load, queues, KV reuse, and service levels | Research |
| Distributed inference | Aggregated serving, Prefill/Decode disaggregation, and WideEP parallelism | Research |
| Control plane | Model services, replica management, autoscaling, updates, and failure recovery | In development |
| [Observability](observability/README.md) | Collect metrics and persistent service logs, evaluate alerts, and inspect the system Dashboard | In development |

## Quick Start

Choose the deployment path before running the common steps:

| Situation | Guide |
|---|---|
| Single-host local deployment | [k3d deployment](docs/deployment/k3d.md) · [kind deployment](docs/deployment/kind.md) |
| Kubernetes deployment with K3s, RKE2, KubeSphere, cloud, or another cluster | [Kubernetes deployment](docs/deployment/kubernetes.md) |
| MetaX GPU deployment | [MetaX deployment](docs/deployment/metax.md) |

The steps below use k3d as the example.

### 1. Get the examples and install the command-line tool

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
uv venv --python 3.12 --seed  # With pip: python3 -m venv .venv (Python 3.11+)
source .venv/bin/activate
uv pip install -e .  # Or: pip install -e .

# Use the published CLI instead:
# uv pip install foretoken  # Or: pip install foretoken
```

Keep this environment active for the commands below.

### 2. Install the Kubernetes platform

Create a local k3d cluster named `foretoken-dev` and install Foretoken:

```bash
# Use GPU index 0 from nvidia-smi. To use two GPUs, pass --gpus 0,1.
foretoken cluster create k3d --name foretoken-dev --gpus 0

# Install the published platform:
foretoken install

# Build from the current source checkout instead:
# foretoken install -e .
```

Install the platform once for the cluster. Each deployment instance has its own controller and services, with a shared Gateway for HTTP access by default.

The host must have Docker, NVIDIA Container Toolkit, k3d, kubectl, and [Helm 4](cli/README.md#before-you-start), and your user must be able to run `docker info` without `sudo`. Install host dependencies separately when needed. For kind or an existing Kubernetes cluster, use the corresponding guide in the table above.

### 3. Deploy the Quick Start

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

Without `--name`, the instance name comes from `metadata.name` of the `FrontendService` in [`frontend.yaml`](examples/quickstart/qwen3-0.6b/frontend.yaml): `qwen3-0-6b` in this example.

The instance contains one frontend service and one `Qwen/Qwen3-0.6B` model replica. The model requests 1 GPU, 4 CPU, and 48 GiB memory, with limits of 8 CPU and 64 GiB. Model files and runtime cache use the platform's default storage, backed by `./data` in this local setup. More deployments are available in [`examples/`](examples/).

For code changes and parallel experiments, use [source deployment](docs/deployment/source.md).

### 4. Send a test request

The Host header selects this instance at the shared Gateway.

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint qwen3-0-6b)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint qwen3-0-6b --host)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

### 5. Evaluate and profile the service

The examples save results locally and to W&B. Run `wandb login` once before using W&B.

#### Performance: latency and throughput

```bash
foretoken perf examples/quickstart/qwen3-0.6b --num-prompts 20 --output local,wandb
```

Read request success, latency, and throughput in the summary. [Performance examples](benchmarks/docs/perf/README.md) cover other workloads and load settings.

#### Quality: score model answers

```bash
foretoken eval examples/quickstart/qwen3-0.6b \
  --evaluator lm-eval --tasks gsm8k --limit 100 --output local,wandb
```

This scores 100 GSM8K math problems. See [Quality evaluation](benchmarks/docs/eval/README.md) for EvalScope, task parameters, and saved scores.

#### Profiling: inspect execution bottlenecks

The Quick Start already configures persistent capture storage. See the [profiling guide](benchmarks/docs/profile/README.md) for tool setup and other capture options.

```bash
foretoken perf examples/quickstart/qwen3-0.6b \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local,wandb
foretoken profile view
```

Open the printed URL to inspect the capture. Press Ctrl+C to close the viewer; the model service remains running.

## Optimize Inference with RSI

Agents investigate optimization methods for a chosen model, workload, and resource budget, change code or configuration, and deploy candidates. Performance measurements, quality evaluations, and execution profiles guide the next iteration. A running experiment report compares approaches and records measured gains and retained changes.

After installing and signing in to Claude Code or Codex, open a session from the repository root. The example below optimizes [Qwen3.5-35B-A3B on NVIDIA GPUs](rsi-playbooks/tasks/qwen3.5-35b-a3b-nvidia.md).

```bash
# Claude Code
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude \
  '/goal Use the RSI skill and an Agent Team to complete rsi-playbooks/tasks/qwen3.5-35b-a3b-nvidia.md.'

# Codex
codex
```

In Codex, enter the following commands separately, reviewing and trusting the project hooks before starting the goal:

```text
/hooks
/goal Use the RSI skill to complete rsi-playbooks/tasks/qwen3.5-35b-a3b-nvidia.md.
```

To pause, use `/goal clear` in Claude Code or `/goal pause` in Codex, and ask the agents to stop this task's experiments and collectors. For more detailed objectives and constraints, fill in the [task template](rsi-playbooks/templates/task-template.md) and reference the task file in your prompt. See the [RSI playbooks](rsi-playbooks/README.md) for the full workflow.

## Gateway Mode

To reuse an existing Gateway or select one of its listeners, see the [installation options](cli/README.md#install).

For a custom domain, HTTPS or a hostname-restricted listener, set the accepted hostname in `examples/quickstart/qwen3-0.6b/frontend.yaml` and redeploy. HTTPS also needs matching DNS and a Gateway certificate:

```yaml
spec:
  hostname: foretoken.example.com
```

## Stop and Uninstall

```bash
# Delete only the Quick Start instance
foretoken delete qwen3-0-6b

# Platform administrator: uninstall when no deployments need the platform
foretoken uninstall
```

The uninstall command preserves Foretoken CRDs, log storage, and reused cluster components. It removes the platform and the monitoring or Gateway resources managed by the command-line tool.

## Related Projects

- [vLLM](https://github.com/vllm-project/vllm)
- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)
- [llm-d](https://github.com/llm-d/llm-d)
- [AIBrix](https://github.com/vllm-project/aibrix)
- [vLLM Production Stack](https://github.com/vllm-project/production-stack)

## Contributing

Contributions of all kinds are welcome, including code, documentation, tests, design discussions, issue reports, and improvements to deployment, hardware, benchmarking, routing, and autoscaling.
Performance-related changes should include the test setup, raw results, and reproducible commands.
See [Contributing to Foretoken](CONTRIBUTING.md) for development principles, collaboration expectations, and the pull request workflow.

Thank you to everyone who has contributed to Foretoken.

<a href="https://github.com/shiweijiezero/foretoken/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=shiweijiezero/foretoken" width="256" alt="Foretoken contributors" />
</a>

## License

This project is licensed under the [Apache License 2.0](LICENSE).

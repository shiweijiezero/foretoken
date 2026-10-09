# Foretoken

English | [简体中文](README_zh.md)

Foretoken is a generative inference orchestration framework built for SLO/SLA targets and heterogeneous accelerators.

Built on inference engines such as vLLM and SGLang, Foretoken organizes multiple generation instances into a cluster service for request routing, autoscaling, instance management, and benchmarking.
We aim to turn an inference cluster into a token factory that continuously converts compute into tokens while meeting latency and quality requirements.

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
| [Evaluation](benchmarks/README.md) | Measure service performance and model quality | In development |
| [Profiling](benchmarks/docs/profile/README.md) | Capture PyTorch, NVIDIA Nsight Systems, or MetaX mcTracer timelines for a model service | In development |
| Hardware support | Common interfaces for device capabilities, runtimes, communication, and metrics; see [MetaX deployment](docs/metax-deployment.md) | In development |
| Request routing | Select instances based on load, queues, KV reuse, and service levels | Research |
| Distributed inference | Aggregated serving, Prefill/Decode disaggregation, and WideEP parallelism | Research |
| Control plane | Model services, replica management, autoscaling, updates, and failure recovery | In development |
| [Observability](observability/README.md) | Collect metrics and persistent service logs, evaluate alerts, and inspect the system Dashboard | In development |

## Quick Start

Choose the deployment path before running the common steps:

| Situation | Guide |
|---|---|
| Single-host local deployment | [k3d deployment](docs/k3d-deployment.md) · [kind deployment](docs/kind-deployment.md) |
| Kubernetes deployment with K3s, RKE2, KubeSphere, cloud, or another cluster | [Kubernetes deployment](docs/kubernetes-deployment.md) |
| MetaX GPU deployment | [MetaX deployment](docs/metax-deployment.md) |

The steps below use k3d as the example.

### 1. Get the examples and install the command-line tool

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
pip install -e .

# For the published CLI instead:
# pip install foretoken
```

### 2. Install the Kubernetes platform

Create a local k3d cluster named `foretoken-dev` and install Foretoken:

```bash
# Use GPU index 0 from nvidia-smi. To use two GPUs, pass --gpus 0,1.
foretoken cluster create k3d --name foretoken-dev --gpus 0

# Build from the current source checkout:
foretoken install -e .

# Use the published platform instead:
# foretoken install
```

The host must have Docker, NVIDIA Container Toolkit, k3d, kubectl, and Helm, and your user must be able to run `docker info` without `sudo`. Install host dependencies separately when needed. For kind or an existing Kubernetes cluster, use the corresponding guide in the table above.

### 3. Deploy the Quick Start

```bash
foretoken deploy examples/quickstart --timeout 20m
```

This example deploys one frontend service and one `Qwen/Qwen3-0.6B` model replica. The model requests 1 GPU, 4 CPU, and 48 GiB memory, with limits of 8 CPU and 64 GiB. The example uses the repository-root `./data` directory for model files and runtime cache. More deployments are available in [`examples/`](examples/).

### 4. Send a test request

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

### Iterate on source

After a source installation, edit the checkout and rerun the deploy command to update the service. See [Deploy Foretoken from Source](docs/custom-deployment.md) for engine checkouts and runtime changes.

### 5. Evaluate and profile the service

The examples save results locally and to W&B. Run `wandb login` once before using W&B.

#### Performance: latency and throughput

```bash
foretoken perf examples/quickstart --num-prompts 20 --output local,wandb
```

Read request success, latency, and throughput in the summary. [Performance examples](benchmarks/docs/perf/README.md) cover other workloads and load settings.

#### Quality: score model answers

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval --tasks gsm8k --limit 100 --output local,wandb
```

This scores 100 GSM8K math problems. See [Quality evaluation](benchmarks/docs/eval/README.md) for EvalScope, task parameters, and saved scores.

#### Profiling: inspect execution bottlenecks

Use a source-installed CLI and platform for profiling, as described in the [profiling guide](benchmarks/docs/profile/README.md). The Quick Start already configures persistent capture storage.

```bash
foretoken perf examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local,wandb
foretoken profile view
```

Open the printed URL to inspect the capture. Press Ctrl+C to close the viewer; the model service remains running.

## Gateway Mode

Gateway mode provides a shared entry point through Kubernetes Gateway and a hostname. It suits clusters that already use Gateway or manage external traffic centrally.

Add the public hostname under `spec` in `examples/quickstart/frontend.yaml`:

```yaml
spec:
  hostname: foretoken.example.com
```

Then run:

```bash
# Install the platform in Gateway mode
foretoken install --frontend-mode gateway
# For a source-installed platform:
# foretoken install -e . --frontend-mode gateway

# Deploy the Quick Start
foretoken deploy examples/quickstart --timeout 20m

# Resolve the Gateway address and request hostname
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"

# Send a test request
curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

The command installs Envoy Gateway when needed. See the [command-line tool guide](cli/README.md) to reuse an existing Gateway or select a listener.

## Stop and Uninstall

```bash
# Delete the Quick Start resources, including its namespace and runtime cache PVC
foretoken delete examples/quickstart

# Uninstall the Foretoken platform
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

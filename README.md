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
| Benchmarking | Performance benchmarks and parameter sweeps, correctness evaluation, and SLO simulation | In development |
| Profiling | Use PyTorch Profiler and Nsight to identify compute, communication, and CPU/GPU bottlenecks | Planned |
| Hardware support | Common interfaces for device capabilities, runtimes, communication, and metrics | In development |
| Request routing | Select instances based on load, queues, KV reuse, and service levels | Research |
| Distributed inference | Aggregated serving, Prefill/Decode disaggregation, and WideEP parallelism | Research |
| Control plane | Model services, instance groups, autoscaling, updates, and failure recovery | In development |
| [Observability](observability/README.md) | Collect runtime metrics, evaluate alerts, and profile CPU/GPU bottlenecks | In development |

## Quick Start

This Quick Start requires Python 3.10 or later, a Kubernetes cluster, `kubectl`, Helm, and at least one available GPU. See the [k3d guide](docs/k3d-deployment.md) to prepare a single-machine test cluster.

### 1. Install the CLI

From the repository root:

```bash
pip install -e .
```

### 2. Install the Kubernetes platform

By default, installation uses the Foretoken images published on GHCR:

```bash
foretoken install
```

This installs the Foretoken CRDs and controller in the `foretoken-platform` namespace and waits for the controller to become ready. The default mode exposes the frontend through a `LoadBalancer` Service.

If you changed the source in this repository, install from source instead:

```bash
foretoken install -e .
```

This rebuilds the images and updates the cluster. To deploy the current source to a remote cluster, see the [source deployment guide](docs/custom-deployment.md).

### 3. Deploy the Quick Start

```bash
foretoken deploy examples/quickstart
```

This example deploys one frontend service and one `Qwen/Qwen3-0.6B` model replica. The workload requests one GPU, 8 CPU, and 52 GiB memory; allow additional capacity for the platform. See the [single-model example](examples/quickstart/README.md) for its resource configuration and [`examples/`](examples/) for more deployments.

### 4. Send a test request

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

### 5. Run a benchmark

```bash
pip install -e '.[bench]'
foretoken bench examples/quickstart
```

See [Benchmarking](benchmarks/README.md) for datasets, remote endpoints, result storage, and parameter sweeps.

## Gateway Mode

Gateway mode provides a shared entry point through Kubernetes Gateway and a hostname. It suits clusters that already use Gateway or manage external traffic centrally.

Foretoken creates its default Gateway for Envoy Gateway. Install Envoy Gateway, then add the public hostname under `spec` in `examples/quickstart/frontend.yaml`:

```yaml
spec:
  hostname: foretoken.example.com
```

Then run:

```bash
# Install Envoy Gateway
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait

# Install the platform in Gateway mode
foretoken install --frontend-mode gateway

# Deploy the Quick Start
foretoken deploy examples/quickstart

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

See the [CLI guide](cli/README.md) to reuse a Gateway from another controller, select a listener, or configure TLS.

## Stop and Uninstall

```bash
# Delete the frontend and model service deployed by the Quick Start
foretoken delete examples/quickstart

# Uninstall the Foretoken platform
foretoken uninstall
```

The uninstall command preserves Foretoken CRDs and reused cluster components. It removes the platform and the monitoring or Gateway resources managed by the CLI.

## Related Projects

- [vLLM](https://github.com/vllm-project/vllm)
- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)
- [llm-d](https://github.com/llm-d/llm-d)
- [AIBrix](https://github.com/vllm-project/aibrix)
- [vLLM Production Stack](https://github.com/vllm-project/production-stack)

## Contributing

Contributions to deployment baselines, hardware support, benchmarking, routing and autoscaling algorithms, tests, and documentation are welcome.
Performance-related changes should include the test setup, raw results, and reproducible commands.
See [Contributing to Foretoken](CONTRIBUTING.md) for development principles, collaboration expectations, and the pull request workflow.

Thank you to everyone who has contributed to Foretoken.

<a href="https://github.com/shiweijiezero/foretoken/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=shiweijiezero/foretoken" alt="Foretoken contributors" />
</a>

## License

This project is licensed under the [Apache License 2.0](LICENSE).

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
- Use the same orchestration stack across NVIDIA, MetaX, Huawei Ascend, and other accelerators.

If you only need to serve a single model on one GPU, using an inference engine such as vLLM directly is usually enough.

## Features and Status

| Feature | Description | Status |
|---|---|---|
| Benchmarking | Performance benchmarks and parameter sweeps, correctness evaluation, and SLO simulation | In development |
| Profiling | Use PyTorch Profiler and Nsight to identify compute, communication, and CPU/GPU bottlenecks | Planned |
| Hardware support | Common interfaces for device capabilities, runtimes, communication, and metrics | In development |
| Request routing | Select instances based on load, queues, KV reuse, and service levels | Research |
| Distributed inference | Aggregated serving, Prefill/Decode disaggregation, and WideEP parallelism | Research |
| Control plane | Model services, instance groups, autoscaling, updates, and failure recovery | Planned |
| [Observability](observability/README.md) | Collect runtime metrics, evaluate alerts, and profile CPU/GPU bottlenecks | In development |

## Quick Start

Foretoken supports local and gateway access modes. Local mode exposes the frontend directly through a `LoadBalancer` Service and suits local or lab clusters. Gateway mode provides a shared hostname through the Gateway API and suits clusters that already use Kubernetes Gateway or need centralized ingress management. After choosing a mode, the service deployment and benchmark steps are the same.

### 1. Install Foretoken

Install the CLI from the repository root with pip:

```bash
pip install -e .
```

Or create and activate a virtual environment with uv:

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

This step only installs the `foretoken` command in the current Python environment; it does not change the Kubernetes cluster. The CLI installs the Foretoken Chart version that matches its package version; run `foretoken --version` to see that release version.

#### Local mode

Use the CLI to install the Foretoken platform into the cluster selected by the active `kubectl` context. CLI-managed platform resources use the `foretoken-platform` namespace. The default local mode exposes the frontend through a `LoadBalancer` Service. Run the same command again to update the existing installation:

```bash
foretoken install
```

#### Gateway mode

First set the public hostname under `spec` in `examples/quickstart/frontend.yaml`:

```yaml
spec:
  hostname: foretoken.example.com
```

Gateway mode requires a Gateway Controller. If the cluster does not already have one, this example installs Envoy Gateway:

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait
```

Create a dedicated `GatewayClass` and `Gateway` for Foretoken:

```bash
foretoken install --frontend-mode gateway
```

To reuse an existing Gateway, list its name and namespace:

```bash
kubectl get gateway -A
```

Then install Foretoken with that Gateway and listener:

```bash
foretoken install \
  --frontend-mode gateway \
  --gateway-name inference-gateway \
  --gateway-namespace gateway-system \
  --gateway-section-name https
```

The Gateway must allow `HTTPRoute` resources from the frontend namespace; DNS and TLS remain owned by the platform Gateway.

### 2. Deploy the model service

`examples/quickstart` provides a ready-to-use frontend and single-model configuration. For two models with queue-based autoscaling, see [Multi-Model Quick Start](examples/multi-model-quickstart/README.md).

```bash
foretoken deploy examples/quickstart
```

The command applies the Kustomize configuration, reports each service state as it changes, and exits when the current configuration is ready.

### 3. Send a generation request

#### Local mode

Resolve the frontend URL and send the request:

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"quickstart-qwen3-0.6b","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

#### Gateway mode

For the Chart-created HTTP Gateway, resolve its address and routing hostname:

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"quickstart-qwen3-0.6b","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

When reusing a platform Gateway, use that Gateway's configured hostname, port, and TLS settings.

### 4. Benchmark service throughput

Install the optional benchmark dependencies from the repository root with pip:

```bash
pip install -e '.[bench]'
```

Or install the benchmark dependencies in the activated uv environment:

```bash
uv pip install -e '.[bench]'
```

The benchmark command reuses an existing Quick Start service. If the service is absent, it deploys the configuration and removes only the resources it created after the benchmark. When neither `--prompt` nor `--dataset` is specified, it uses a short built-in prompt:

```bash
foretoken bench examples/quickstart
```

Results are shown in the console by default. Use `--output local` to save local artifacts, `--output wandb` to publish the run, or combine them.

To benchmark an already running OpenAI-compatible service instead:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --prompt "Hello"
```

## Stop and Uninstall

Delete the serving configuration so the Operator can stop the service and clean up its resources:

```bash
foretoken delete examples/quickstart
```

After the serving resources are gone, uninstall the platform:

```bash
foretoken uninstall
```

`foretoken uninstall` removes the platform and its CLI-managed monitoring and Gateway resources; reused cluster components remain unchanged.

If Envoy Gateway was installed only for this Foretoken deployment, uninstall it as well:

```bash
helm uninstall envoy-gateway \
  --namespace envoy-gateway-system \
  --wait --timeout 5m
```

Do not run this step while other services still use Envoy Gateway.

Uninstalling the control plane preserves Foretoken CRDs and custom resources. Delete the CRDs explicitly only after all Foretoken resources have been removed:

```bash
kubectl delete crd \
  frontendservices.inference.foretoken.io \
  kvservices.inference.foretoken.io \
  kvpools.inference.foretoken.io \
  kvgroups.inference.foretoken.io \
  modelservices.inference.foretoken.io \
  modelpools.inference.foretoken.io \
  modelgroups.inference.foretoken.io
```

## Development Deployments

To build and deploy local source changes, import images into a cluster, or distribute development images through an OCI registry, see [Deploy Foretoken from Source](docs/custom-deployment.md).

To create an isolated single-machine Kubernetes cluster with a selected set of GPUs, see [Deploy Foretoken with k3d](docs/k3d-deployment.md).

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

## License

This project is licensed under the [Apache License 2.0](LICENSE).

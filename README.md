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
| Deployment and observability | Kubernetes deployment, metrics, dashboards, and alerts | Planned |

## Quick Start

Foretoken supports local and gateway access modes. Local mode exposes the frontend directly through a `LoadBalancer` Service and suits local or lab clusters. Gateway mode provides a shared hostname through the Gateway API and suits clusters that already use Kubernetes Gateway or need centralized ingress management. After choosing a mode, the service deployment and benchmark steps are the same.

### 1. Install Foretoken

#### Local mode

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=local \
  --wait \
  --debug
```

#### Gateway mode

First set the public hostname under `spec` in `examples/quickstart/frontend.yaml`:

```yaml
spec:
  hostname: foretoken.example.com
```

Gateway mode requires a Gateway Controller. This example installs Envoy Gateway:

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait \
  --debug
```

Then let the Foretoken Chart create a dedicated `GatewayClass` and `Gateway`:

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=gateway \
  --set frontend.gateway.create=true \
  --wait \
  --debug
```

If the platform already has a suitable `Gateway`, first list its name and namespace:

```bash
kubectl get gateway -A
```

For example:

```text
NAMESPACE        NAME
gateway-system   inference-gateway
```

To reuse this Gateway, install Foretoken with the complete command below:

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=gateway \
  --set frontend.gateway.name=inference-gateway \
  --set frontend.gateway.namespace=gateway-system \
  --set frontend.gateway.sectionName=https \
  --wait \
  --debug
```

`name` comes from the `NAME` column, `namespace` from `NAMESPACE`, and `sectionName` is the listener name selected from that Gateway. The Gateway must allow `HTTPRoute` resources from the frontend namespace; DNS and TLS remain owned by the platform Gateway.

### 2. Deploy the model service

`examples/quickstart` provides a ready-to-use frontend and model service configuration:

```bash
kubectl apply --server-side -k examples/quickstart
```

### 3. Wait for serving to become ready

```bash
kubectl wait --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m \
  frontendservice/quickstart-frontend \
  modelservice/quickstart-qwen3-0.6b
```

### 4. Send a generation request

#### Local mode

Read the frontend address and send the request:

```bash
kubectl wait --for=jsonpath='{.status.loadBalancer.ingress}' \
  --namespace foretoken-demo \
  --timeout=5m \
  service/quickstart-frontend

FORETOKEN_FRONTEND_ADDRESS=$(kubectl get service quickstart-frontend \
  --namespace foretoken-demo \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')

curl --fail-with-body --no-buffer \
  "http://${FORETOKEN_FRONTEND_ADDRESS}:8080/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"quickstart-qwen3-0.6b","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

#### Gateway mode

For the Chart-created HTTP Gateway, read its address and send the configured hostname:

```bash
FORETOKEN_GATEWAY_ADDRESS=$(kubectl get gateway foretoken-gateway \
  --namespace foretoken-platform \
  -o jsonpath='{.status.addresses[0].value}')

curl --fail-with-body --no-buffer \
  "http://${FORETOKEN_GATEWAY_ADDRESS}/v1/chat/completions" \
  -H "Host: foretoken.example.com" \
  -H "Content-Type: application/json" \
  -d '{"model":"quickstart-qwen3-0.6b","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

When reusing a platform Gateway, use that Gateway's configured hostname, port, and TLS settings.

### 5. Benchmark service throughput

Install the Foretoken Benchmark CLI from this project path:

```bash
python -m pip install ./benchmarks
```

After the service is ready, the command uses the same deployment configuration to find its endpoint and model. With no workload options, it uses a short built-in prompt:

```bash
foretoken bench examples/quickstart
```

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
kubectl delete --wait=true --timeout=10m \
  -k examples/quickstart
```

After the serving resources are gone, uninstall Foretoken:

```bash
helm uninstall foretoken \
  --namespace foretoken-platform \
  --wait --timeout 5m
```

A `GatewayClass` and `Gateway` created with `frontend.gateway.create=true` are removed with the Foretoken release; a reused platform Gateway is left unchanged.

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

## Deploy from Source

To validate local source changes or build and manage your own Foretoken images, choose one of the following image distribution methods. `make dev-deploy` builds the source images and installs or updates the Foretoken platform; deploy the Quick Start with separate commands. See [Deploy Foretoken from Source](docs/custom-deployment.md) for complete instructions.

### Import local images directly

Use Kind to create the cluster, import the images, and deploy the platform when validating the control plane, CRDs, frontend, and scheduling behavior:

```bash
KIND_CLUSTER=foretoken-local make dev-deploy
```

To run a GPU model service, select the available GPUs as described in [Deploy Foretoken with k3d](docs/k3d-deployment.md), then run:

```bash
CLUSTER=foretoken-local GPU_INDICES=0 make dev-deploy
```

### Distribute through an OCI registry

An OCI registry can distribute locally built images to Kubernetes nodes:

```bash
export GITHUB_USER=your-github-user
export REGISTRY="ghcr.io/$GITHUB_USER/foretoken-dev"
docker login ghcr.io
REGISTRY="$REGISTRY" make dev-deploy
```

After the platform deployment completes, explicitly run the Quick Start commands under “Deploy the model service” and “Wait for serving to become ready” to start the example frontend and model service.

## Deploy with k3d

To create an isolated single-machine cluster for development and testing with a selected set of GPUs, see [Deploy Foretoken with k3d](docs/k3d-deployment.md).

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

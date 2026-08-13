# Foretoken

English | [简体中文](README_zh.md)

Foretoken is a Kubernetes-native GPU generative inference orchestration framework built for SLO/SLA targets.

Currently built on vLLM, Foretoken organizes model runtimes into services with request routing, autoscaling, rollout, graceful drain, and benchmarking. We aim to turn an inference cluster into a token factory that continuously converts compute into tokens while meeting latency and quality requirements.

## When to Use Foretoken

- Serve one or more models across multiple GPUs or nodes.
- Route requests using health, current load, and optional KV cache signals.
- Autoscale model capacity from frontend queue depth and model-server active requests.
- Explore aggregated serving, Prefill/Decode disaggregation, and parallelism strategies.
- Manage runtime and node scheduling through a platform-configured vLLM GPU runtime profile.

If you only need to serve a single model on one GPU, using vLLM directly is usually enough.

## Features and Status

| Feature | Description | Status |
|---|---|---|
| Kubernetes control plane | ModelService, ModelPool, ModelGroup, rollout, scaling, and graceful drain | In development |
| Request routing | Route lowered requests to compatible and healthy ModelGroups | In development |
| vLLM integration | Reuse vLLM Rust tokenization, lowering, EngineCore client, streams, and detokenization | In development |
| Benchmarking | Single-point OpenAI-compatible load tests with latency, TTFT, TPOT, and throughput metrics | In development |
| GPU support | Platform-configured vLLM runtime, GPU resource, and node scheduling profile | In development |
| Distributed inference | Aggregate serving is implemented; Prefill/Decode disaggregation remains experimental | Research |
| Observability | Frontend metrics and autoscaling telemetry; dashboards, distributed tracing, and alerts are planned | In development |

## Quick Start

Foretoken runs as one Kubernetes system. Platform administrators configure the Gateway and current vLLM GPU runtime profile through the Chart; serving users submit `FrontendService` and `ModelService` resources without starting individual processes.

### Prerequisites

- Kubernetes 1.29+ with GPU nodes and cluster-scoped installation permissions;
- Kubernetes Gateway API and a Gateway that permits Routes from serving namespaces;

### 1. Install Foretoken

Install Foretoken with the official Helm Chart:

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --version 0.0.1 \
  --namespace foretoken-platform \
  --create-namespace \
  --wait
```

The Chart uses matching official control-plane, frontend, and model-server images. See [Build from Source](#build-from-source) to deploy custom images.


### 2. Deploy a model service

`examples/quickstart` declares both the API-serving frontend and the model service. Foretoken creates and manages the underlying Pools, Groups, Deployments, Services, routes, and runtime configuration.

```bash
FORETOKEN_NAMESPACE=foretoken-demo

kubectl create namespace "${FORETOKEN_NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply --server-side \
  --namespace "${FORETOKEN_NAMESPACE}" \
  -k examples/quickstart
```

Replace `foretoken.example.com` in `examples/quickstart/frontend.yaml` with a hostname routed to the platform Gateway.

### 3. Wait for serving readiness

```bash
kubectl wait frontendservice/quickstart-frontend \
  --for=condition=Ready \
  --namespace "${FORETOKEN_NAMESPACE}" \
  --timeout=15m

kubectl wait modelservice/quickstart-qwen3-0.6b \
  --for=condition=Ready \
  --namespace "${FORETOKEN_NAMESPACE}" \
  --timeout=15m
```

`FrontendService` becomes Ready after its frontend Deployment is available, its HTTPRoute is accepted with resolved references by the Gateway, and a routable serving configuration is installed. `ModelService` becomes Ready when every ModelPool with requested serving capacity is Ready. During rollout or capacity convergence, a ModelPool can remain Ready while its active revision still has at least one Ready ModelGroup.

### 4. Send a request through the Gateway

```bash
# Use the externally reachable address of the configured Gateway listener.
FORETOKEN_BASE_URL=https://foretoken.example.com

curl --fail-with-body --no-buffer \
  "${FORETOKEN_BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

## Stop and Uninstall

Delete serving intent first so Foretoken can stop and clean up the owned resources:

```bash
kubectl delete --wait=true --timeout=10m \
  --namespace "${FORETOKEN_NAMESPACE}" \
  -k examples/quickstart
```

Then uninstall Foretoken:

```bash
helm uninstall foretoken \
  --namespace foretoken-platform \
  --wait --timeout=5m
```

Foretoken CRDs are preserved during a normal uninstall. Delete them explicitly only after all Foretoken custom resources have been removed:

```bash
kubectl delete crd \
  frontendservices.inference.foretoken.io \
  modelservices.inference.foretoken.io \
  modelpools.inference.foretoken.io \
  modelgroups.inference.foretoken.io \
  kvservices.inference.foretoken.io \
  kvpools.inference.foretoken.io \
  kvgroups.inference.foretoken.io
```

## Build from Source

After modifying Foretoken, build custom images and deploy them with the official Chart. Frontend and model-server builds require a local vLLM Git checkout:

```bash
FORETOKEN_VLLM_SOURCE=/path/to/vllm make image-frontend
FORETOKEN_VLLM_SOURCE=/path/to/vllm \
  VLLM_RUNTIME_IMAGE=<vLLM runtime image> \
  make image-model-server

docker build -f control-plane/Dockerfile \
  -t foretoken-control-plane:dev .
```

Tag the images with your registry and push them, for example:

```bash
REGISTRY=registry.example.com/foretoken

docker tag foretoken-frontend:dev "${REGISTRY}/frontend:dev"
docker tag foretoken-model-server:dev "${REGISTRY}/model-server:dev"
docker tag foretoken-control-plane:dev "${REGISTRY}/control-plane:dev"

docker push "${REGISTRY}/frontend:dev"
docker push "${REGISTRY}/model-server:dev"
docker push "${REGISTRY}/control-plane:dev"
```

Install with the custom images:

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --version 0.0.1 \
  --namespace foretoken-platform \
  --create-namespace \
  --set image.repository="${REGISTRY}/control-plane" \
  --set image.tag=dev \
  --set frontend.image="${REGISTRY}/frontend:dev" \
  --set runtime.vllm.image="${REGISTRY}/model-server:dev" \
  --wait
```

If you changed only one component, build, push, and override only that component's image.

## Related Projects

- [vLLM](https://github.com/vllm-project/vllm)
- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)
- [llm-d](https://github.com/llm-d/llm-d)
- [AIBrix](https://github.com/vllm-project/aibrix)
- [vLLM Production Stack](https://github.com/vllm-project/production-stack)

## Contributing

Contributions to deployment baselines, hardware support, benchmarking, routing and autoscaling algorithms, tests, and documentation are welcome. Performance-related changes should include the test setup, raw results, and reproducible commands.

See [Contributing to Foretoken](CONTRIBUTING.md) for development principles, collaboration expectations, and the pull request workflow.

## License

This project is licensed under the [Apache License 2.0](LICENSE).

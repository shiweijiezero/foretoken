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
| [Benchmarking](benchmarks/README.md) | Existing-endpoint and Kubernetes-managed load tests with latency, TTFT, TPOT, and throughput metrics | In development |
| GPU support | Platform-configured vLLM runtime, GPU resource, and node scheduling profile | In development |
| Distributed inference | Aggregate serving is implemented; Prefill/Decode disaggregation remains experimental | Research |
| Observability | Frontend metrics and autoscaling telemetry; dashboards, distributed tracing, and alerts are planned | In development |

## Quick Start

Foretoken runs as one Kubernetes system. A platform administrator installs the control plane once; serving users deploy `FrontendService` and `ModelService` resources without starting the underlying processes themselves.

The first OCI release and matching component images have not been published yet. The current source checkout therefore uses the local Chart and three images from a registry reachable by every cluster node.

### Prerequisites

- Kubernetes 1.29+ with GPU nodes and cluster-scoped installation permissions;
- a working GPU device plugin and the node label used by the selected runtime profile;
- Kubernetes Gateway API and a Gateway that allows Routes from `foretoken-demo`;
- a registry that cluster workloads can pull from.

### 1. Build and push the images

The first data-plane `make` command initializes the official vLLM submodule and applies the generic Rust API extension required by Foretoken; no manual vLLM edits are needed. The model-server runtime image must provide a compatible vLLM Python runtime.

```bash
REGISTRY=registry.example.com/foretoken
VLLM_RUNTIME_IMAGE=registry.example.com/vllm/runtime:tag

make image-frontend
make image-model-server VLLM_RUNTIME_IMAGE="${VLLM_RUNTIME_IMAGE}"
docker build -f control-plane/Dockerfile -t foretoken-control-plane:dev .

docker tag foretoken-control-plane:dev "${REGISTRY}/control-plane:dev"
docker tag foretoken-frontend:dev "${REGISTRY}/frontend:dev"
docker tag foretoken-model-server:dev "${REGISTRY}/model-server:dev"

docker push "${REGISTRY}/control-plane:dev"
docker push "${REGISTRY}/frontend:dev"
docker push "${REGISTRY}/model-server:dev"
```

To initialize the source before building, run `git submodule update --init data-plane/third_party/vllm`. All workload nodes must be able to pull the three pushed images without per-Pod registry credentials.

### 2. Configure the platform

Copy the example and replace the three image paths, Gateway parent, and GPU profile with values that exist in your cluster:

```bash
cp deploy/platform-values.example.yaml /tmp/foretoken-platform-values.yaml
${EDITOR:-vi} /tmp/foretoken-platform-values.yaml
```

If `kubectl get runtimeclass` shows a GPU runtime such as `nvidia`, set `runtime.vllm.accelerator.runtimeClassName` to that name. Leave it empty when the cluster injects GPU devices without a RuntimeClass.

This is a one-time platform configuration. Model authors do not repeat it for each service.

### 3. Install Foretoken

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --values /tmp/foretoken-platform-values.yaml \
  --wait \
  --timeout=5m
```

The Chart rejects an incomplete frontend, Gateway, or GPU runtime profile before creating the control-plane Deployment.

### 4. Deploy the example service

Before applying the example, replace `foretoken.example.com` in `examples/quickstart/frontend.yaml` with a hostname accepted by the configured Gateway listener. Then submit the namespace, frontend, and model service together:

```bash
kubectl apply --server-side -k examples/quickstart
```

Foretoken creates and manages the underlying Pools, Groups, Deployments, Services, routes, and runtime configuration.

### 5. Wait for readiness and send a request

```bash
kubectl wait frontendservice/quickstart-frontend \
  --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m

kubectl wait modelservice/quickstart-qwen3-0.6b \
  --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m

FORETOKEN_BASE_URL=https://foretoken.example.com

curl --fail-with-body --no-buffer \
  "${FORETOKEN_BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

`FrontendService` becomes Ready after its Deployment is available, its HTTPRoute is accepted by the Gateway, and a routable backend is installed. `ModelService` becomes Ready after its requested ModelGroups can serve requests.

## Stop and Uninstall

Delete serving intent first so Foretoken can stop and clean up the resources it owns:

```bash
kubectl delete --wait=true --timeout=10m -k examples/quickstart
```

Then uninstall the platform release:

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

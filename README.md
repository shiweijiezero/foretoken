# Foretoken

English | [简体中文](README_zh.md)

Foretoken is a generative model inference orchestration framework built for SLO/SLA targets.

Foretoken organizes model-serving instances into one cluster service for request routing, autoscaling, rolling updates, graceful draining, and benchmarking.

We aim to turn an inference cluster into a token factory that continuously converts compute into tokens while meeting latency requirements.

```text
Resource management: ModelService → ModelPool → ModelGroup
Request path:       Client → Gateway → FrontendService → ModelGroup → model-server → inference engine
```

## When to Use Foretoken

- Manage one or more model services in a Kubernetes cluster.
- Route requests using backend health, load, and opportunities to reuse KV cache.
- Use autoscaling to adjust model-serving capacity from queued and in-progress requests.
- Explore aggregate inference, Prefill/Decode disaggregation, and parallelism strategies.

If you only need to serve a single model on one GPU, using vLLM directly is usually enough.

## Current Capabilities

Foretoken is under active development. Its APIs and deployment configuration may continue to change.

| Capability | Description |
|---|---|
| Service management | Create and manage model workloads from `ModelService`, including scaling, updates, and draining |
| Request routing | Select an available `ModelGroup` by model, health, and configured routing policy |
| Autoscaling | Adjust the number of complete `ModelGroup` instances from queued and in-progress requests |
| Data plane | Provide an OpenAI-compatible API with request processing, backend selection, and streaming or non-streaming responses |
| [Benchmarking](benchmarks/README.md) | Test an existing service or temporarily deploy Foretoken services in Kubernetes for a load test |
| Distributed inference | Aggregate inference is implemented; Prefill/Decode disaggregation remains experimental |
| [Observability](observability/README.md) | Frontend and model-server expose OpenMetrics; the Helm chart can add discovery, recording rules, and an overview dashboard to an existing monitoring stack |

## Deploy from Source

This flow uses the Helm chart in this repository and pushes three images—control-plane, frontend, and model-server—to a registry the cluster can pull from. A platform administrator installs the control plane once. Service operators then create `FrontendService` and `ModelService` resources instead of deploying frontend or model-server workloads directly.

### Prerequisites

The build machine needs:

- a Git checkout of Foretoken, plus Docker, GNU Make, Helm, and kubectl;
- kubectl configured for the target cluster;
- access to the Git submodule, container base images, and a compatible inference engine image.

The cluster needs:

- Kubernetes 1.29+, GPU nodes, and cluster-scoped installation permissions;
- a working GPU device plugin and node labels that identify the target GPU nodes;
- Kubernetes Gateway API and a Gateway that accepts `HTTPRoute` resources from `foretoken-demo`;
- workload nodes that can pull all three Foretoken component images;
- access to the Qwen model and tokenizer used by the example, unless the runtime already provides those files.

The Quick Start requests 4 CPUs, 48 GiB of memory, and one GPU by default. Adjust `examples/quickstart/model.yaml` first when the cluster cannot schedule those resources.

> The chart currently applies `imagePullSecrets` only to the control-plane Pod. Frontend and model-server Pods created by Foretoken do not inherit those Secrets; when using a private registry, ensure the workload nodes already have pull access.

### 1. Build and push the images

The model-server image is layered on the selected inference engine image. Set `INFERENCE_ENGINE_IMAGE` to a compatible image; the current vLLM adapter requires Python and `vllm.entrypoints.cli.main`.

```bash
REGISTRY=registry.example.com/foretoken
INFERENCE_ENGINE_IMAGE=registry.example.com/inference/engine:tag

make image-frontend
make image-model-server INFERENCE_ENGINE_IMAGE="${INFERENCE_ENGINE_IMAGE}"
docker build -f control-plane/Dockerfile -t foretoken-control-plane:dev .

docker tag foretoken-control-plane:dev "${REGISTRY}/control-plane:dev"
docker tag foretoken-frontend:dev "${REGISTRY}/frontend:dev"
docker tag foretoken-model-server:dev "${REGISTRY}/model-server:dev"

docker push "${REGISTRY}/control-plane:dev"
docker push "${REGISTRY}/frontend:dev"
docker push "${REGISTRY}/model-server:dev"
```

### 2. Configure the platform

Copy the example configuration:

```bash
cp deploy/platform-values.example.yaml /tmp/foretoken-platform-values.yaml
${EDITOR:-vi} /tmp/foretoken-platform-values.yaml
```

Set:

- the control-plane, frontend, and model-server image references;
- the Gateway name and namespace, plus `sectionName` when a specific listener must be selected;
- the GPU type, Kubernetes GPU resource name, and node selector.

When the cluster already runs Prometheus Operator and Grafana, optionally
enable the ServiceMonitors, recording rules, and overview dashboard described
in [Observability](observability/README.md).

If `kubectl get runtimeclass` lists a GPU RuntimeClass such as `nvidia`, set `runtime.vllm.accelerator.runtimeClassName` to that name. Leave it empty when the cluster provides GPUs without a RuntimeClass.

These are platform-level settings; service operators do not repeat them for each model.

### 3. Install Foretoken

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --values /tmp/foretoken-platform-values.yaml \
  --wait \
  --timeout=5m
```

### 4. Deploy the example service

Replace `foretoken.example.com` in `examples/quickstart/frontend.yaml` with a hostname served by the selected Gateway listener, then deploy the example:

```bash
kubectl apply --server-side -k examples/quickstart
```

The example creates one `FrontendService` and one `ModelService` in `foretoken-demo`. Foretoken uses them to create the frontend route and model-serving workloads.

### 5. Wait for readiness and send a request

Wait for the model backend before waiting for the frontend and route:

```bash
kubectl wait modelservice/quickstart-qwen3-0.6b \
  --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m

kubectl wait frontendservice/quickstart-frontend \
  --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m

FORETOKEN_BASE_URL=https://foretoken.example.com

curl --fail-with-body --no-buffer \
  "${FORETOKEN_BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

Change `https` to the actual `http` or `https` scheme used by the Gateway listener. `ModelService` Ready means the model backend can serve requests. `FrontendService` Ready means its replicas, Gateway route, and routable backend are available.

If either wait command times out, inspect status and events first:

```bash
kubectl describe modelservice/quickstart-qwen3-0.6b -n foretoken-demo
kubectl describe frontendservice/quickstart-frontend -n foretoken-demo
```

## Benchmarking

[Foretoken Benchmark](benchmarks/README.md) can test an existing OpenAI-compatible service or temporarily deploy services into a Kubernetes cluster with the Foretoken control plane installed. Managed mode also needs a usable StorageClass, a benchmark image, and a Gateway that accepts `HTTPRoute` resources from temporary namespaces.

## Stop and Uninstall

Delete the example `FrontendService` and `ModelService` first. Their controllers remove the workloads they manage and preserve the configured drain time for model groups:

```bash
kubectl delete --wait=true --timeout=10m -k examples/quickstart
```

Then uninstall the platform:

```bash
helm uninstall foretoken \
  --namespace foretoken-platform \
  --wait --timeout=5m
```

A normal uninstall preserves Foretoken CRDs. Delete these cluster-scoped API definitions only after confirming that all Foretoken custom resources have been removed:

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

Foretoken currently uses vLLM as its inference engine. The following projects provide related production-inference and Kubernetes-orchestration approaches:

- [vLLM](https://github.com/vllm-project/vllm)
- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)
- [llm-d](https://github.com/llm-d/llm-d)
- [AIBrix](https://github.com/vllm-project/aibrix)
- [vLLM Production Stack](https://github.com/vllm-project/production-stack)

## Contributing

Contributions are welcome. For development setup, contribution expectations, and reproducibility requirements for performance changes, see [Contributing to Foretoken](CONTRIBUTING.md).

## License

This project is licensed under the [Apache License 2.0](LICENSE).

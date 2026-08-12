# Foretoken

English | [简体中文](README_zh.md)

Foretoken is a Kubernetes-native generative inference orchestration framework built for SLO/SLA targets and heterogeneous accelerators.

Built on inference engines such as vLLM and SGLang, Foretoken organizes model runtimes into services with request routing, autoscaling, rollout, failure recovery, and benchmarking. We aim to turn an inference cluster into a token factory that continuously converts compute into tokens while meeting latency and quality requirements.

```text
Control plane: ModelService → ModelPool → ModelGroup
Data plane:    Client → platform-managed Gateway → FrontendService
               → Foretoken Router → selected ModelGroup service
               → group-local model-server → backend EngineCore
```

## When to Use Foretoken

- Serve one or more models across multiple accelerators or nodes.
- Route requests based on load, queue depth, KV cache state, and service objectives.
- Autoscale model capacity based on traffic and SLO targets.
- Compare aggregated serving, Prefill/Decode disaggregation, and parallelism strategies.
- Use one orchestration model across NVIDIA, MetaX, Huawei Ascend, and other accelerators.

If you only need to serve a single model on one accelerator, using an inference engine such as vLLM directly is usually enough.

## Features and Status

| Feature | Description | Status |
|---|---|---|
| Kubernetes control plane | ModelService, ModelPool, ModelGroup, rollout, scaling, and failure recovery | In development |
| Request routing | Route lowered requests to compatible model groups | In development |
| vLLM integration | Reuse vLLM Rust tokenization, lowering, EngineCore client, streams, and detokenization | In development |
| Benchmarking | Performance sweeps, correctness evaluation, and SLO simulation | In development |
| Hardware support | Common runtime and scheduling profiles for heterogeneous accelerators | In development |
| Distributed inference | Aggregated serving, Prefill/Decode disaggregation, and WideEP | Research |
| Observability | Metrics, dashboards, tracing, and alerts | Planned |

## Quick Start

Foretoken runs as one Kubernetes system. Platform administrators configure the Gateway and accelerator runtime profiles once; serving users submit `FrontendService` and `ModelService` resources without starting individual processes.

### Prerequisites

- A Kubernetes cluster with accelerator nodes and the corresponding device plugin.
- Gateway API v1 CRDs and a platform-managed Gateway whose listener permits routes from serving namespaces.
- DNS for the serving hostname, or an equivalent test route to the Gateway.
- Cluster access to the Foretoken control-plane, frontend, and model-server OCI images.

### 1. Install Foretoken

Install the local Chart after preparing images in a registry accessible to the cluster, as described in [Source and Private Deployments](#source-and-private-deployments):

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --values platform-values.yaml \
  --wait
```

`platform-values.yaml` is owned by the platform team. For the complete Frontend Quick Start, it must set `frontend.enabled=true` and provide the required `frontend.image` and `frontend.gateway.name`, `frontend.gateway.namespace`, and `frontend.gateway.sectionName` values, in addition to the model-server runtime, accelerator resource, and node scheduling profile. Private and offline environments can point the same values at mirrored OCI artifacts.

The official `0.0.1` OCI Chart is not yet available for anonymous pulls. Use the local Chart or a private registry until the official OCI Chart is published.

### 2. Deploy a model service

`examples/quickstart` declares both the northbound frontend and the model service. Foretoken creates and manages the underlying Pools, Groups, Deployments, Services, routes, and runtime configuration.

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
  --namespace foretoken-demo \
  --timeout=15m

kubectl wait modelservice/quickstart-qwen3-0.6b \
  --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m
```

`FrontendService` becomes Ready when its frontend Deployment is available, its HTTPRoute is accepted and resolved by the Gateway, and a routable backend snapshot is installed. `ModelService` becomes Ready when every serving ModelPool has a routable active revision. A Pool remains Ready while at least one active Group is ready; `CapacityReady` reports whether all requested Groups are ready.

### 4. Send a request through the Gateway

```bash
curl --fail-with-body --no-buffer \
  https://foretoken.example.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

## Stop and Uninstall

Delete serving intent first so Foretoken can stop and clean up the owned resources:

```bash
kubectl delete --wait=true --timeout=10m \
  --namespace foretoken-demo \
  -k examples/quickstart
```

Then uninstall the control plane:

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

## Source and Private Deployments

Kubernetes runs OCI images rather than the source checkout directly. Source, private-registry, and offline deployments use the same architecture:

1. Build the control-plane, frontend, and model-server OCI images from this repository.
2. Publish them to an OCI registry accessible to the cluster, or import them directly into development cluster nodes.
3. Set their immutable references and the cluster runtime profile in Helm values.
4. Install the local Chart with `helm upgrade --install foretoken ./deploy/charts/foretoken --namespace foretoken-platform --create-namespace --values <values-file>`.

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

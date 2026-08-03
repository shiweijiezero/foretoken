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

### 1. Install Foretoken

Install Foretoken with Helm:

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --wait
```

### 2. Deploy a model service

`serving_demo.yaml` declares the traffic entrypoint and model service configuration. The Operator creates and manages the underlying resources.

```bash
FORETOKEN_NAMESPACE=foretoken-demo
FORETOKEN_SERVING_CONFIG=deploy/examples/quickstart/serving_demo.yaml

# Create the namespace for the model service; keep it unchanged if it exists.
kubectl create namespace "${FORETOKEN_NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Apply the model service configuration; the Operator creates and starts its workloads.
kubectl apply --server-side \
  --namespace "${FORETOKEN_NAMESPACE}" \
  -f "${FORETOKEN_SERVING_CONFIG}"
```

### 3. Wait for serving to become ready

```bash
FORETOKEN_NAMESPACE=foretoken-demo
FORETOKEN_SERVING_CONFIG=deploy/examples/quickstart/serving_demo.yaml

# Wait until the model service and traffic entrypoint in the configuration are ready.
kubectl wait --for=condition=Ready \
  --namespace "${FORETOKEN_NAMESPACE}" \
  --timeout=10m \
  -f "${FORETOKEN_SERVING_CONFIG}"
```

### 4. Send a generation request through the Gateway

```bash
curl --fail-with-body --no-buffer \
  https://foretoken.example.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

Users submit the serving configuration. The Operator manages the underlying resources, and clients access the model service through the Gateway.

## Stop and Uninstall

Delete the serving configuration so the Operator can stop the service and clean up its resources:

```bash
FORETOKEN_NAMESPACE=foretoken-demo
FORETOKEN_SERVING_CONFIG=deploy/examples/quickstart/serving_demo.yaml

kubectl delete --wait=true --timeout=10m \
  --namespace "${FORETOKEN_NAMESPACE}" \
  -f "${FORETOKEN_SERVING_CONFIG}"
```

After the serving resources are gone, uninstall Foretoken:

```bash
helm uninstall foretoken \
  --namespace foretoken-platform \
  --wait --timeout 5m
```

## Install from Source

Use the local Chart from a source checkout:

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --wait
```

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

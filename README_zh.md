# Foretoken

[English](README.md) | 简体中文

Foretoken 是一个面向 SLO/SLA 与异构硬件的生成式推理编排框架。

Foretoken 基于 vLLM、SGLang 等推理引擎，把多个生成实例组织成一套集群服务，负责请求路由、自动扩缩容、实例管理和性能评测。
我们希望将推理集群转化为 Token 工厂，把算力持续转化为满足延迟和质量要求的 token。

## 什么时候需要 Foretoken

- 在多卡或多节点上运行一种或多种模型。
- 根据负载、队列或 KV Cache 状态路由请求。
- 根据请求量和 SLO 自动扩缩推理实例。
- 比较聚合部署、Prefill/Decode 分离和不同并行方案。
- 在 NVIDIA、沐曦、昇腾等不同硬件上使用同一套编排方案。

如果只在单张卡上运行一个模型，直接使用 vLLM 等推理引擎通常就够了。

## 功能与进展

| 功能 | 说明 | 状态 |
|---|---|---|
| 评测 | 性能压测与参数扫描、正确性评测和 SLO 仿真 | 开发中 |
| Profiling | PyTorch Profiler 和 Nsight 定位计算、通信及 CPU/GPU 性能瓶颈 | 规划中 |
| 硬件适配 | 统一设备能力、运行时、通信和指标接口 | 开发中 |
| 请求路由 | 基于负载、队列、KV 复用和服务等级选择实例 | 研究中 |
| 分布式推理 | 聚合部署、Prefill/Decode 分离和 WideEP 并行策略 | 研究中 |
| 控制面 | 模型服务、实例组、扩缩容、更新和故障恢复 | 规划中 |
| 部署与观测 | Kubernetes 部署、指标、仪表盘和告警 | 规划中 |

## 快速开始

Foretoken 支持本地模式和网关模式。本地模式通过 `LoadBalancer` Service 直接提供 frontend 地址，适合本地集群或实验室环境；网关模式通过 Gateway API 和域名提供统一入口，适合已经使用 Kubernetes Gateway 或需要统一管理对外流量的集群。选择一种模式安装后，后续服务部署和评测步骤相同。

### 1. 安装 Foretoken

#### 本地模式

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

#### 网关模式

先在 `examples/quickstart/frontend.yaml` 的 `spec` 中填写对外域名：

```yaml
spec:
  hostname: foretoken.example.com
```

网关模式需要 Gateway Controller。以下示例安装 Envoy Gateway：

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait \
  --debug
```

然后让 Foretoken Chart 创建专用的 `GatewayClass` 和 `Gateway`：

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

如果平台已经有可用的 `Gateway`，可以先查看它的名称和 namespace：

```bash
kubectl get gateway -A
```

例如输出：

```text
NAMESPACE        NAME
gateway-system   inference-gateway
```

如果复用这个 Gateway，使用以下完整命令安装 Foretoken：

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

其中 `name` 对应 `NAME` 列，`namespace` 对应 `NAMESPACE` 列，`sectionName` 是该 Gateway 中目标 listener 的名称。该 Gateway 必须允许前端服务所在 namespace 的 `HTTPRoute` 接入；DNS 和 TLS 继续由平台网关管理。

### 2. 部署模型服务

`examples/quickstart` 提供一套可直接使用的 frontend 和单模型配置。如需运行双模型并验证 queue autoscaling，请参阅 [多模型 Quick Start](examples/multi-model-quickstart/README_zh.md)。

```bash
kubectl apply --server-side -k examples/quickstart
```

### 3. 等待服务就绪

```bash
kubectl wait --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m \
  frontendservice/quickstart-frontend \
  modelservice/quickstart-qwen3-0.6b
```

### 4. 发送生成请求进行测试

#### 本地模式

读取前端服务的访问地址并发送请求：

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
  -d '{"model":"quickstart-qwen3-0.6b","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

#### 网关模式

使用 Chart 创建的 HTTP 网关时，读取网关地址并携带配置的域名：

```bash
FORETOKEN_GATEWAY_ADDRESS=$(kubectl get gateway foretoken-gateway \
  --namespace foretoken-platform \
  -o jsonpath='{.status.addresses[0].value}')

curl --fail-with-body --no-buffer \
  "http://${FORETOKEN_GATEWAY_ADDRESS}/v1/chat/completions" \
  -H "Host: foretoken.example.com" \
  -H "Content-Type: application/json" \
  -d '{"model":"quickstart-qwen3-0.6b","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

复用平台已有网关时，使用该网关实际配置的域名、端口和 TLS。

### 5. 评测服务吞吐

先从当前项目路径安装 Foretoken Benchmark CLI：

```bash
python -m pip install ./benchmarks
```

服务就绪后，以下命令会根据同一份部署配置自动找到访问地址和模型。未指定 workload 时使用一个简短的内置 prompt：

```bash
foretoken bench examples/quickstart
```

如果要评测已经运行的 OpenAI API 兼容服务，则显式提供地址和模型：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --prompt "Hello"
```

## 停止与卸载

```bash
# 删除服务配置，停止服务并清理所辖资源：
kubectl delete --wait=true --timeout=10m \
  -k examples/quickstart

# 服务资源清理完成后，再卸载 Foretoken：
helm uninstall foretoken \
  --namespace foretoken-platform \
  --wait --timeout 5m
```

通过 `frontend.gateway.create=true` 创建的 `GatewayClass` 和 `Gateway` 会随 Foretoken release 一起删除；复用的平台网关不会被删除。

如果 Envoy Gateway 仅供本次 Foretoken 部署使用，可以继续卸载它：

```bash
helm uninstall envoy-gateway \
  --namespace envoy-gateway-system \
  --wait --timeout 5m
```

其他服务仍在使用 Envoy Gateway 时不要执行这一步。

卸载 control plane 时会保留 Foretoken CRD 和自定义资源。只有在清理全部 Foretoken 资源后，才应显式删除 CRD：

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

## 从源码部署

需要验证本地源码修改，或希望自行构建和管理 Foretoken 镜像时，可以选择以下镜像分发方式。`make dev-deploy` 构建源码镜像并安装或更新 Foretoken 平台；Quick Start 使用独立命令部署。完整说明参见 [从源码部署 Foretoken](docs/custom-deployment_zh.md)。

### 直接导入本地镜像

使用 Kind 验证 control-plane、CRD、frontend 和调度逻辑时，创建 cluster、导入镜像并部署平台：

```bash
KIND_CLUSTER=foretoken-local make dev-deploy
```

需要运行 GPU 模型服务时，按 [使用 k3d 部署 Foretoken](docs/k3d-deployment_zh.md) 指定可用 GPU，然后运行：

```bash
CLUSTER=foretoken-local GPU_INDICES=0 make dev-deploy
```

### 通过 OCI registry 分发

OCI registry 可以将本地构建的镜像分发给 Kubernetes 节点：

```bash
export GITHUB_USER=your-github-user
export REGISTRY="ghcr.io/$GITHUB_USER/foretoken-dev"
docker login ghcr.io
REGISTRY="$REGISTRY" make dev-deploy
```

平台部署完成后，如需启动示例 frontend 和模型服务，显式执行上方 Quick Start 中的“部署模型服务”和“等待服务就绪”命令。

## 使用 k3d 部署

希望在一台机器上为开发测试快速创建独立 cluster，并限定该 cluster 可以使用的 GPU 时，请参阅 [使用 k3d 部署 Foretoken](docs/k3d-deployment_zh.md)。

## 相关项目

- [vLLM](https://github.com/vllm-project/vllm)
- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)
- [llm-d](https://github.com/llm-d/llm-d)
- [AIBrix](https://github.com/vllm-project/aibrix)
- [vLLM Production Stack](https://github.com/vllm-project/production-stack)

## 贡献

欢迎贡献部署基线、硬件适配、Benchmark、路由算法、扩缩容算法、测试和文档。
性能相关变更需要附上测试条件、原始结果和可重复执行的命令。
开发原则、协作约定和 Pull Request 流程见 [《为 Foretoken 做贡献》](CONTRIBUTING_zh.md)。

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 许可证。

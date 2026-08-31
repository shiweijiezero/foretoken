# Foretoken

[English](README.md) | 简体中文

Foretoken 是一个面向 SLO/SLA 与异构硬件的生成式推理编排框架。

Foretoken 基于 vLLM、SGLang 等推理引擎，把多个生成实例组织成一套集群服务，负责请求路由、自动扩缩容、实例管理和性能评测。
我们希望将推理集群转化为 Token 工厂，把算力持续转化为满足延迟和质量要求的 Token。

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
| 性能剖析 | PyTorch Profiler 和 Nsight 定位计算、通信及 CPU/GPU 性能瓶颈 | 规划中 |
| 硬件适配 | 统一设备能力、运行时、通信和指标接口 | 开发中 |
| 请求路由 | 基于负载、队列、KV 复用和服务等级选择实例 | 研究中 |
| 分布式推理 | 聚合部署、Prefill/Decode 分离和 WideEP 并行策略 | 研究中 |
| 控制面 | 模型服务、实例组、扩缩容、更新和故障恢复 | 规划中 |
| [可观测性](observability/README_zh.md) | 采集运行指标、评估告警并分析 CPU/GPU 性能瓶颈 | 开发中 |

## 快速开始

Foretoken 支持本地模式和网关模式。本地模式通过 `LoadBalancer` Service 直接提供前端服务地址，适合本地集群或实验室环境；网关模式通过 Gateway API 和域名提供统一入口，适合已经使用 Kubernetes Gateway 或需要统一管理对外流量的集群。安装时选择其中一种模式，后续的服务部署和评测步骤相同。

### 1. 安装 Foretoken

#### 本地模式

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=local \
  --wait
```

#### 网关模式

先在 `examples/quickstart/frontend.yaml` 的 `spec` 中填写对外域名：

```yaml
spec:
  hostname: foretoken.example.com
```

网关模式需要网关控制器。以下示例安装 Envoy Gateway：

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait
```

然后使用 Foretoken Chart 创建专用的 `GatewayClass` 和 `Gateway`：

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=gateway \
  --set frontend.gateway.create=true \
  --wait
```

如果平台已经有可用的 `Gateway`，可以先查看其名称和命名空间：

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
  --wait
```

其中 `name` 对应 `NAME` 列，`namespace` 对应 `NAMESPACE` 列，`sectionName` 是该 Gateway 中目标监听器的名称。该 Gateway 必须允许前端服务所在命名空间的 `HTTPRoute` 接入；DNS 和 TLS 继续由平台网关管理。

### 2. 部署模型服务

先在仓库根目录安装 Foretoken CLI。`examples/quickstart` 提供一套可直接使用的前端服务和单模型配置。如需运行双模型并验证基于队列的自动扩缩容，请参阅[多模型快速开始](examples/multi-model-quickstart/README_zh.md)。

```bash
pip install -e .
foretoken deploy examples/quickstart
```

该命令会应用 Kustomize 配置，在服务状态变化时输出进度，并在当前配置就绪后退出。

### 3. 发送生成请求进行测试

#### 本地模式

解析前端服务 URL 并发送请求：

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"quickstart-qwen3-0.6b","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

#### 网关模式

使用由 Chart 创建的 HTTP 网关时，解析网关地址和路由域名：

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"quickstart-qwen3-0.6b","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

复用平台已有网关时，使用该网关实际配置的域名、端口和 TLS。

### 4. 评测服务吞吐

在仓库根目录安装可选的评测依赖：

```bash
pip install -e '.[bench]'
```

以下命令会复用已经运行的快速开始服务；服务尚未部署时，CLI 会创建配置中的资源，并在评测结束后只清理本次创建的资源。未指定 `--prompt` 或 `--dataset` 时，使用一个简短的内置提示词：

```bash
foretoken bench examples/quickstart
```

默认仅在控制台显示结果。使用 `--output local` 将结果保存到本地，使用 `--output wandb` 发布到 W&B；两者也可同时使用。

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
foretoken delete examples/quickstart

# 服务资源清理完成后，再卸载 Foretoken：
helm uninstall foretoken \
  --namespace foretoken-platform \
  --wait --timeout 5m
```

通过 `frontend.gateway.create=true` 创建的 `GatewayClass` 和 `Gateway` 会随 Foretoken 的 Helm 发布实例一同删除；复用的平台网关不会被删除。

如果 Envoy Gateway 仅供本次 Foretoken 部署使用，可以继续卸载它：

```bash
helm uninstall envoy-gateway \
  --namespace envoy-gateway-system \
  --wait --timeout 5m
```

其他服务仍在使用 Envoy Gateway 时不要执行这一步。

卸载控制平面时会保留 Foretoken CRD 和自定义资源。只有在清理全部 Foretoken 资源后，才应显式删除 CRD：

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

## 开发部署

如需构建和部署本地源码修改、向集群导入镜像，或通过 OCI 镜像仓库分发开发镜像，请参阅[从源码部署 Foretoken](docs/custom-deployment_zh.md)。

如需在单台机器上创建相互隔离的 Kubernetes 集群并指定可用 GPU，请参阅[使用 k3d 部署 Foretoken](docs/k3d-deployment_zh.md)。

## 相关项目

- [vLLM](https://github.com/vllm-project/vllm)
- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)
- [llm-d](https://github.com/llm-d/llm-d)
- [AIBrix](https://github.com/vllm-project/aibrix)
- [vLLM Production Stack](https://github.com/vllm-project/production-stack)

## 贡献

欢迎贡献部署基线、硬件适配、性能评测、路由算法、扩缩容算法、测试和文档。
性能相关变更需要附上测试条件、原始结果和可重复执行的命令。
开发原则、协作约定和 Pull Request 流程见 [《为 Foretoken 做贡献》](CONTRIBUTING_zh.md)。

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 许可证。

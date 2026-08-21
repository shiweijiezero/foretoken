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

两种访问模式使用相同的部署、等待和卸载步骤，只需在安装 Foretoken 时选择一种模式。

### 1. 安装 Foretoken

#### 本地模式

本地模式通过 `LoadBalancer` 提供访问地址，集群需要支持 `LoadBalancer` Service：

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

网关模式需要 Gateway Controller。以下示例安装 Envoy Gateway：

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait
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
  --wait
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

此时不设置 `frontend.gateway.create=true`，而是在上述 Foretoken 安装命令中改用：

```bash
--set frontend.gateway.name=inference-gateway \
--set frontend.gateway.namespace=gateway-system \
--set frontend.gateway.sectionName=https
```

其中 `name` 对应 `NAME` 列，`namespace` 对应 `NAMESPACE` 列，`sectionName` 是该 Gateway 中目标 listener 的名称。该 Gateway 必须允许前端服务所在 namespace 的 `HTTPRoute` 接入；DNS 和 TLS 继续由平台网关管理。

### 2. 部署模型服务

`examples/quickstart/kustomization.yaml` 是部署入口，统一组织前端服务和模型服务：

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

## 从源码安装

使用源码目录中的本地 Chart：

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=local \
  --wait
```

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

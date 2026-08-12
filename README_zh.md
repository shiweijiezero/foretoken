# Foretoken

[English](README.md) | 简体中文

Foretoken 是一个面向 SLO/SLA 与异构加速器的 Kubernetes 原生生成式推理编排框架。

Foretoken 基于 vLLM、SGLang 等推理引擎，把模型运行时组织成一套具备请求路由、自动扩缩容、滚动更新、故障恢复和性能评测能力的服务。我们希望将推理集群转化为 Token 工厂，把算力持续转化为满足延迟和质量要求的 token。

```text
控制面：ModelService → ModelPool → ModelGroup
数据面：Client → 平台托管 Gateway → FrontendService
        → Foretoken Router → 选中的 ModelGroup Service
        → Group-local model-server → backend EngineCore
```

## 什么时候需要 Foretoken

- 在多加速器或多节点上运行一种或多种模型。
- 根据负载、队列、KV Cache 状态和服务目标路由请求。
- 根据流量和 SLO 自动扩缩模型容量。
- 比较聚合部署、Prefill/Decode 分离和不同并行策略。
- 在 NVIDIA、沐曦、昇腾等不同硬件上使用同一套编排模型。

如果只在单个加速器上运行一个模型，直接使用 vLLM 等推理引擎通常就够了。

## 功能与进展

| 功能 | 说明 | 状态 |
|---|---|---|
| Kubernetes 控制面 | ModelService、ModelPool、ModelGroup、更新、扩缩和故障恢复 | 开发中 |
| 请求路由 | 将 lowering 后的请求路由到兼容的模型 Group | 开发中 |
| vLLM 集成 | 复用 vLLM Rust 的 tokenization、lowering、EngineCore client、stream 和 detokenization | 开发中 |
| 评测 | 性能参数扫描、正确性评测和 SLO 仿真 | 开发中 |
| 硬件支持 | 面向异构加速器的统一 runtime 与调度 profile | 开发中 |
| 分布式推理 | 聚合部署、Prefill/Decode 分离和 WideEP | 研究中 |
| 可观测性 | 指标、仪表盘、追踪和告警 | 规划中 |

## 快速开始

Foretoken 作为一套完整的 Kubernetes 系统运行。平台管理员统一配置 Gateway 和加速器 runtime profile；服务用户只需提交 `FrontendService` 和 `ModelService`，不需要分别启动底层进程。

### 前置条件

- 具备加速器节点及对应 device plugin 的 Kubernetes 集群。
- Gateway API v1 CRD，以及 listener 允许服务 namespace 挂载 Route 的平台托管 Gateway。
- 指向 Gateway 的服务域名，或等价的测试访问入口。
- 集群能够访问 Foretoken control-plane、frontend 和 model-server OCI 镜像。

### 1. 安装 Foretoken

按[源码与私有化部署](#源码与私有化部署)准备好集群可访问 registry 中的镜像后，使用本地 Chart 安装：

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --values platform-values.yaml \
  --wait
```

`platform-values.yaml` 由平台团队维护。要完成 Frontend 快速开始，必须设置 `frontend.enabled=true`，并提供必填的 `frontend.image` 以及 `frontend.gateway.name`、`frontend.gateway.namespace` 和 `frontend.gateway.sectionName`；此外还需配置 model-server runtime、加速器资源和节点调度 profile。私有化和离线环境可以通过同一份 values 指向内部同步的 OCI artifacts。

官方 `0.0.1` OCI Chart 目前尚不能匿名拉取。在官方 OCI Chart 发布前，请使用本地 Chart 或私有 registry。

### 2. 部署模型服务

`examples/quickstart` 同时声明北向 frontend 和模型服务。Foretoken 自动创建并管理底层 Pool、Group、Deployment、Service、路由和运行时配置。

```bash
FORETOKEN_NAMESPACE=foretoken-demo

kubectl create namespace "${FORETOKEN_NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply --server-side \
  --namespace "${FORETOKEN_NAMESPACE}" \
  -k examples/quickstart
```

请将 `examples/quickstart/frontend.yaml` 中的 `foretoken.example.com` 替换为指向平台 Gateway 的域名。

### 3. 等待服务就绪

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

`FrontendService` 在 frontend Deployment 可用、HTTPRoute 已被 Gateway 接受并解析，且已安装可路由的后端快照后进入 Ready。`ModelService` 在每个承载流量的 ModelPool 都有可路由的 active revision 后进入 Ready。只要至少一个 active Group 已就绪，Pool 就会保持 Ready；`CapacityReady` 表示全部期望 Group 是否已就绪。

### 4. 通过 Gateway 发送请求

```bash
curl --fail-with-body --no-buffer \
  https://foretoken.example.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 停止与卸载

先删除服务意图，让 Foretoken 停止并清理所辖资源：

```bash
kubectl delete --wait=true --timeout=10m \
  --namespace foretoken-demo \
  -k examples/quickstart
```

再卸载控制面：

```bash
helm uninstall foretoken \
  --namespace foretoken-platform \
  --wait --timeout=5m
```

正常卸载时会保留 Foretoken CRD。只有在全部 Foretoken 自定义资源清理完成后，才应显式删除 CRD：

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

## 源码与私有化部署

Kubernetes 运行 OCI 镜像，不会直接运行源码目录。源码、私有 registry 和离线部署使用相同架构：

1. 从本仓库构建 control-plane、frontend 和 model-server OCI 镜像。
2. 将镜像发布到集群可访问的 OCI registry，或直接导入开发集群节点。
3. 在 Helm values 中设置不可变镜像引用和集群 runtime profile。
4. 使用 `helm upgrade --install foretoken ./deploy/charts/foretoken --namespace foretoken-platform --create-namespace --values <values-file>` 安装本地 Chart。

## 相关项目

- [vLLM](https://github.com/vllm-project/vllm)
- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)
- [llm-d](https://github.com/llm-d/llm-d)
- [AIBrix](https://github.com/vllm-project/aibrix)
- [vLLM Production Stack](https://github.com/vllm-project/production-stack)

## 贡献

欢迎贡献部署基线、硬件适配、Benchmark、路由和自动扩缩算法、测试及文档。性能相关变更需要附上测试条件、原始结果和可重复执行的命令。

开发原则、协作约定和 Pull Request 流程见 [《为 Foretoken 做贡献》](CONTRIBUTING_zh.md)。

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 许可证。

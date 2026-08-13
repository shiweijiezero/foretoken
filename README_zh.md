# Foretoken

[English](README.md) | 简体中文

Foretoken 是一个面向 SLO/SLA 的 Kubernetes 原生 GPU 生成式推理编排框架。

Foretoken 当前基于 vLLM，把模型运行时组织成一套具备请求路由、自动扩缩容、滚动更新、优雅排空和性能评测能力的服务。我们希望将推理集群转化为 Token 工厂，把算力持续转化为满足延迟和质量要求的 token。

## 什么时候需要 Foretoken

- 在多 GPU 或多节点上运行一种或多种模型。
- 根据健康状态、当前负载和可选 KV Cache 信号路由请求。
- 根据 frontend 队列和 model-server 活跃请求自动扩缩模型容量。
- 探索聚合部署、Prefill/Decode 分离和不同并行策略。
- 通过平台配置的 vLLM GPU runtime profile 管理运行时与节点调度。

如果只在单个 GPU 上运行一个模型，直接使用 vLLM 通常就够了。

## 功能与进展

| 功能 | 说明 | 状态 |
|---|---|---|
| Kubernetes 控制面 | ModelService、ModelPool、ModelGroup、滚动更新、扩缩和优雅排空 | 开发中 |
| 请求路由 | 将 lowering 后的请求路由到兼容且健康的 ModelGroup | 开发中 |
| vLLM 集成 | 复用 vLLM Rust 的 tokenization、lowering、EngineCore client、stream 和 detokenization | 开发中 |
| 评测 | 单负载点 OpenAI 兼容压测，统计延迟、TTFT、TPOT 和吞吐 | 开发中 |
| GPU 支持 | 平台配置的 vLLM runtime、GPU 资源与节点调度 profile | 开发中 |
| 分布式推理 | 聚合部署已实现；Prefill/Decode 分离仍在实验和验证中 | 研究中 |
| 可观测性 | frontend 指标与自动扩缩遥测；仪表盘、分布式追踪和告警仍在规划中 | 开发中 |

## 快速开始

Foretoken 作为一套完整的 Kubernetes 系统运行。平台管理员通过 Chart 配置 Gateway 和当前 vLLM GPU runtime profile；服务用户只需提交 `FrontendService` 和 `ModelService`，不需要分别启动底层进程。

### 前置条件

- Kubernetes 1.29+，具备 GPU 节点和集群级安装权限；
- 已安装 Kubernetes Gateway API，并提供允许服务 namespace 挂载 Route 的 Gateway；

### 1. 安装 Foretoken

使用官方 Helm Chart 安装 Foretoken：

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --version 0.0.1 \
  --namespace foretoken-platform \
  --create-namespace \
  --wait
```

Chart 会使用同版本的官方 control-plane、frontend 和 model-server 镜像。自定义镜像的部署方法见[从源码构建](#从源码构建)。


### 2. 部署模型服务

`examples/quickstart` 同时声明对外提供 API 的 frontend 和模型服务。Foretoken 自动创建并管理底层 Pool、Group、Deployment、Service、路由和运行时配置。

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
  --namespace "${FORETOKEN_NAMESPACE}" \
  --timeout=15m

kubectl wait modelservice/quickstart-qwen3-0.6b \
  --for=condition=Ready \
  --namespace "${FORETOKEN_NAMESPACE}" \
  --timeout=15m
```

frontend Deployment 可用、HTTPRoute 已被 Gateway 接受且引用解析完成，并安装可路由的服务配置后，`FrontendService` 进入 Ready。每个具有请求服务容量的 ModelPool 都进入 Ready 后，`ModelService` 进入 Ready。滚动更新或容量收敛期间，只要 active revision 中至少一个 ModelGroup 仍为 Ready，ModelPool 就可以保持 Ready。

### 4. 通过 Gateway 发送请求

```bash
# 使用已配置 Gateway listener 的对外访问地址。
FORETOKEN_BASE_URL=https://foretoken.example.com

curl --fail-with-body --no-buffer \
  "${FORETOKEN_BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 停止与卸载

先删除服务意图，让 Foretoken 停止并清理所辖资源：

```bash
kubectl delete --wait=true --timeout=10m \
  --namespace "${FORETOKEN_NAMESPACE}" \
  -k examples/quickstart
```

再卸载 Foretoken：

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

## 从源码构建

自定义修改 Foretoken 代码后，可以构建自己的镜像并通过官方 Chart 部署。Frontend 和 model-server 构建需要本地 vLLM Git checkout：

```bash
FORETOKEN_VLLM_SOURCE=/path/to/vllm make image-frontend
FORETOKEN_VLLM_SOURCE=/path/to/vllm \
  VLLM_RUNTIME_IMAGE=<vLLM runtime 镜像> \
  make image-model-server

docker build -f control-plane/Dockerfile \
  -t foretoken-control-plane:dev .
```

给镜像加上 registry 地址并推送，例如：

```bash
REGISTRY=registry.example.com/foretoken

docker tag foretoken-frontend:dev "${REGISTRY}/frontend:dev"
docker tag foretoken-model-server:dev "${REGISTRY}/model-server:dev"
docker tag foretoken-control-plane:dev "${REGISTRY}/control-plane:dev"

docker push "${REGISTRY}/frontend:dev"
docker push "${REGISTRY}/model-server:dev"
docker push "${REGISTRY}/control-plane:dev"
```

安装时指定自定义镜像：

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

只修改了其中一个组件时，只需要构建、推送并覆盖该组件的镜像。

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

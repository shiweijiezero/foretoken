# Foretoken

[English](README.md) | 简体中文

Foretoken 是一个面向 SLO/SLA 的 Kubernetes 原生 GPU 生成式推理编排框架。

Foretoken 把多个模型推理实例组织成一套集群服务，负责请求路由、自动扩缩容、实例更新与排空和性能评测。当前支持 vLLM 后端。

我们希望将推理集群转化为 Token 工厂，把算力持续转化为满足延迟要求的 token。

```text
资源管理：ModelService → ModelPool → ModelGroup
请求路径：Client → Gateway → FrontendService → ModelGroup → model-server / vLLM
```

## 什么时候需要 Foretoken

- 在 Kubernetes 集群中统一管理一个或多个模型服务。
- 根据模型实例的健康状态、负载和 KV Cache 复用机会路由请求。
- 启用自动扩缩容后，根据等待处理和正在执行的请求数量增减模型服务容量。
- 探索聚合式推理、Prefill/Decode 分离和不同并行策略。

如果只在单个 GPU 上运行一个模型，直接使用 vLLM 通常就够了。

## 当前能力

Foretoken 仍处于开发阶段，接口和部署配置可能继续调整。

| 能力 | 说明 |
|---|---|
| 服务管理 | 根据 `ModelService` 创建并管理模型工作负载，支持扩缩、更新和排空 |
| 请求路由 | 根据模型、健康状态和配置的路由策略选择可用的 `ModelGroup` |
| 自动扩缩容 | 根据请求队列和正在执行的请求调整完整 `ModelGroup` 数量 |
| 数据面 | 提供 OpenAI 兼容接口，处理请求、选择后端并返回流式或非流式响应 |
| [性能评测](benchmarks/README_zh.md) | 测试已有服务，或在 Kubernetes 中临时部署 Foretoken 服务后进行负载测试 |
| 分布式推理 | 聚合式推理已实现；Prefill/Decode 分离仍在实验和验证中 |
| 可观测性 | frontend 提供运行指标；仪表盘、分布式追踪和告警仍在规划中 |

## 从源码部署

以下流程使用仓库中的 Helm Chart，并将 control-plane、frontend 和 model-server 三张镜像推送到集群能够拉取的 registry。平台管理员只需安装一次控制面；服务使用者随后创建 `FrontendService` 和 `ModelService`，无需直接部署 frontend 或 model-server。

### 前置条件

构建机器需要：

- 通过 Git 获取的 Foretoken 源码，以及 Docker、GNU Make、Helm 和 kubectl；
- kubectl 已连接目标集群；
- 能够访问 Git submodule、容器基础镜像，以及一张与当前源码兼容的 vLLM Python runtime 镜像。

集群需要：

- Kubernetes 1.29+、GPU 节点和集群级安装权限；
- 可用的 GPU device plugin，以及能够识别目标 GPU 节点的标签；
- Kubernetes Gateway API，以及允许 `foretoken-demo` 命名空间创建 `HTTPRoute` 的 Gateway；
- 工作负载节点能够拉取 Foretoken 的三张组件镜像；
- 能够下载示例使用的 Qwen 模型和 tokenizer，或已通过运行环境提供这些文件。

Quick Start 默认请求 4 个 CPU、48 GiB 内存和 1 个 GPU。资源不足时，请先调整 `examples/quickstart/model.yaml`。

> Chart 的 `imagePullSecrets` 当前只用于 control-plane。Foretoken 创建的 frontend 和 model-server Pod 不会继承这些 Secret；使用私有 registry 时，请确保工作负载节点已经具备拉取权限。

### 1. 构建并推送镜像

首次执行 data-plane 镜像构建命令时，Makefile 会自动准备 Foretoken 使用的 vLLM 源码。`VLLM_RUNTIME_IMAGE` 必须包含 Python 和 `vllm.entrypoints.cli.main`，并与当前源码兼容。

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

### 2. 配置平台

复制示例配置：

```bash
cp deploy/platform-values.example.yaml /tmp/foretoken-platform-values.yaml
${EDITOR:-vi} /tmp/foretoken-platform-values.yaml
```

在文件中设置：

- control-plane、frontend 和 model-server 的镜像地址；
- Gateway 的名称、namespace，以及需要指定具体 listener 时使用的 `sectionName`；
- GPU 类型、Kubernetes GPU resource name 和 node selector。

如果 `kubectl get runtimeclass` 能看到 `nvidia` 等 GPU RuntimeClass，请将 `runtime.vllm.accelerator.runtimeClassName` 设置为对应名称；如果集群不依赖 RuntimeClass 提供 GPU，则保持为空。

这些是平台级配置，不需要为每个模型服务重复填写。

### 3. 安装 Foretoken

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --values /tmp/foretoken-platform-values.yaml \
  --wait \
  --timeout=5m
```

### 4. 部署示例服务

将 `examples/quickstart/frontend.yaml` 中的 `foretoken.example.com` 替换为目标 Gateway listener 服务的域名，然后部署示例：

```bash
kubectl apply --server-side -k examples/quickstart
```

示例会在 `foretoken-demo` 命名空间中创建一个 `FrontendService` 和一个 `ModelService`。Foretoken 会据此创建前端路由和模型推理工作负载。

### 5. 等待就绪并发送请求

先等待模型后端，再等待前端和路由：

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
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

请根据 Gateway listener 将 `https` 改为实际使用的 `http` 或 `https`。`ModelService` 进入 Ready 表示模型后端已经可以服务请求；`FrontendService` 进入 Ready 表示前端副本、Gateway 路由和可路由后端都已可用。

如果等待超时，可以先查看状态和事件：

```bash
kubectl describe modelservice/quickstart-qwen3-0.6b -n foretoken-demo
kubectl describe frontendservice/quickstart-frontend -n foretoken-demo
```

## 性能评测

[Foretoken Benchmark](benchmarks/README_zh.md) 可以测试已有的 OpenAI 兼容服务，也可以在已安装 Foretoken 控制面的 Kubernetes 集群中临时部署服务后进行测试。托管模式还需要可用的 StorageClass、Benchmark 镜像，以及允许临时命名空间创建 `HTTPRoute` 的 Gateway。

## 停止与卸载

先删除示例中的 `FrontendService` 和 `ModelService`。控制器会清理它们所管理的工作负载，并为模型组保留配置的排空时间：

```bash
kubectl delete --wait=true --timeout=10m -k examples/quickstart
```

再卸载平台：

```bash
helm uninstall foretoken \
  --namespace foretoken-platform \
  --wait --timeout=5m
```

正常卸载会保留 Foretoken CRD。只有确认所有 Foretoken 自定义资源都已删除后，才应删除这些集群级 API 定义：

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

## 相关项目

Foretoken 当前使用 vLLM 作为推理后端。以下项目提供相关的生产推理和 Kubernetes 编排方案：

- [vLLM](https://github.com/vllm-project/vllm)
- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)
- [llm-d](https://github.com/llm-d/llm-d)
- [AIBrix](https://github.com/vllm-project/aibrix)
- [vLLM Production Stack](https://github.com/vllm-project/production-stack)

## 贡献

欢迎贡献。开发环境、协作约定，以及性能变更的可复现性要求见 [《为 Foretoken 做贡献》](CONTRIBUTING_zh.md)。

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 许可证。

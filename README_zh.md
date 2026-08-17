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
| [评测](benchmarks/README_zh.md) | 测试已有 endpoint，或在 Kubernetes 中临时部署后压测，统计延迟、TTFT、TPOT 和吞吐 | 开发中 |
| GPU 支持 | 平台配置的 vLLM runtime、GPU 资源与节点调度 profile | 开发中 |
| 分布式推理 | 聚合部署已实现；Prefill/Decode 分离仍在实验和验证中 | 研究中 |
| 可观测性 | frontend 指标与自动扩缩遥测；仪表盘、分布式追踪和告警仍在规划中 | 开发中 |

## 快速开始

Foretoken 作为一套完整的 Kubernetes 系统运行。平台管理员只需安装一次控制面；服务用户通过 `FrontendService` 和 `ModelService` 部署服务，不需要分别启动底层进程。

首个 OCI Chart 和配套组件镜像尚未发布。当前源码版本使用本地 Chart，以及集群中每个节点都能拉取的三张组件镜像。

### 前置条件

- Kubernetes 1.29+，具备 GPU 节点和集群级安装权限；
- 可用的 GPU device plugin，以及 runtime profile 使用的节点标签；
- Kubernetes Gateway API，以及允许 `foretoken-demo` 挂载 Route 的 Gateway；
- 集群工作负载可以拉取镜像的 registry。

### 1. 构建并推送镜像

第一次执行 data-plane 的 `make` 命令时，会自动初始化官方 vLLM submodule，并应用 Foretoken 所需的通用 Rust API 扩展，不需要手动修改 vLLM。model-server 的运行时镜像必须包含兼容的 vLLM Python runtime。

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

想在构建前准备好源码，可以先执行 `git submodule update --init data-plane/third_party/vllm`。集群的所有工作负载节点都必须能拉取这三张镜像，且当前运行时不注入 Pod 级 registry 凭据。

### 2. 配置平台

复制示例文件，并将三张镜像、Gateway parent 和 GPU profile 替换为集群中的真实配置：

```bash
cp deploy/platform-values.example.yaml /tmp/foretoken-platform-values.yaml
${EDITOR:-vi} /tmp/foretoken-platform-values.yaml
```

如果 `kubectl get runtimeclass` 能看到 `nvidia` 等 GPU runtime，请将 `runtime.vllm.accelerator.runtimeClassName` 填为对应名称；如果集群不依赖 RuntimeClass 注入 GPU 设备，则保持为空。

这是平台管理员的一次性配置，不需要模型服务用户为每个服务重复填写。

### 3. 安装 Foretoken

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --values /tmp/foretoken-platform-values.yaml \
  --wait \
  --timeout=5m
```

如果 frontend、Gateway 或 GPU runtime profile 不完整，Chart 会在创建控制面 Deployment 前给出明确错误。

### 4. 部署示例服务

应用示例前，先将 `examples/quickstart/frontend.yaml` 中的 `foretoken.example.com` 替换为配置的 Gateway listener 接受的真实域名。然后一次性提交 namespace、frontend 和模型服务：

```bash
kubectl apply --server-side -k examples/quickstart
```

Foretoken 会自动创建并管理底层 Pool、Group、Deployment、Service、路由和运行时配置。

### 5. 等待就绪并发送请求

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
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

frontend Deployment 可用、HTTPRoute 被 Gateway 接受且安装了可路由后端后，`FrontendService` 进入 Ready。请求的 ModelGroup 可以提供服务后，`ModelService` 进入 Ready。

## 停止与卸载

先删除服务意图，让 Foretoken 停止并清理所辖资源：

```bash
kubectl delete --wait=true --timeout=10m -k examples/quickstart
```

再卸载平台：

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

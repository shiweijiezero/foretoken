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
- 在 NVIDIA 和沐曦硬件上使用同一套编排方案。

如果只在单张卡上运行一个模型，直接使用 vLLM 等推理引擎通常就够了。

## 功能与进展

| 功能 | 说明 | 状态 |
|---|---|---|
| 评测 | 性能压测与参数扫描、正确性评测和 SLO 仿真 | 开发中 |
| 性能剖析 | PyTorch Profiler 和 Nsight 定位计算、通信及 CPU/GPU 性能瓶颈 | 规划中 |
| 硬件适配 | 统一设备能力、运行时、通信和指标接口 | 开发中 |
| 请求路由 | 基于负载、队列、KV 复用和服务等级选择实例 | 研究中 |
| 分布式推理 | 聚合部署、Prefill/Decode 分离和 WideEP 并行策略 | 研究中 |
| 控制面 | 模型服务、实例组、扩缩容、更新和故障恢复 | 开发中 |
| [可观测性](observability/README_zh.md) | 采集运行指标、评估告警并分析 CPU/GPU 性能瓶颈 | 开发中 |

## 快速开始

本快速开始需要 Python 3.10 或更高版本、Kubernetes 集群、`kubectl`、Helm 和至少一块可用 GPU。如需在单台机器上准备测试集群，请参阅 [k3d 指南](docs/k3d-deployment_zh.md)。

### 1. 安装 CLI

在仓库根目录运行：

```bash
pip install -e .
```

### 2. 安装 Kubernetes 平台

默认使用 Foretoken 发布在 GHCR 的镜像：

```bash
foretoken install
```

该命令会在 `foretoken-platform` 命名空间中安装 Foretoken CRD 和控制器，并等待控制器就绪。默认模式通过 `LoadBalancer` 类型的 Kubernetes `Service` 提供前端地址。

如果修改了当前仓库中的源码，请改用源码安装：

```bash
foretoken install -e .
```

该命令会重新构建镜像并更新集群。如果要将当前源码部署到远程集群，请参阅[源码部署指南](docs/custom-deployment_zh.md)。

### 3. 部署快速开始示例

```bash
foretoken deploy examples/quickstart
```

该示例部署一个前端服务和一个 `Qwen/Qwen3-0.6B` 模型副本。工作负载请求 1 张 GPU、8 个 CPU 和 52 GiB 内存；还需为平台预留额外容量。资源配置见[单模型示例](examples/quickstart/README_zh.md)，更多部署配置见 [`examples/`](examples/) 目录。

### 4. 发送测试请求

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### 5. 运行评测

```bash
pip install -e '.[bench]'
foretoken bench examples/quickstart
```

数据集、远程服务、结果保存和参数扫描见[评测指南](benchmarks/README_zh.md)。

## 网关模式

网关模式通过 Kubernetes Gateway 和域名提供统一入口，适合已经使用 Gateway 或需要集中管理外部流量的集群。

Foretoken 默认创建的 Gateway 使用 Envoy Gateway。先安装 Envoy Gateway，并在 `examples/quickstart/frontend.yaml` 的 `spec` 中添加访问域名：

```yaml
spec:
  hostname: foretoken.example.com
```

然后运行：

```bash
# 安装 Envoy Gateway
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait

# 安装平台并启用网关模式
foretoken install --frontend-mode gateway

# 部署快速开始示例
foretoken deploy examples/quickstart

# 获取网关地址和请求域名
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"

# 发送测试请求
curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

要复用其他 Gateway Controller 管理的 Gateway、指定 listener 或配置 TLS，见 [CLI 使用指南](cli/README_zh.md)。

## 停止与卸载

```bash
# 删除快速开始部署的前端和模型服务
foretoken delete examples/quickstart

# 卸载 Foretoken 平台
foretoken uninstall
```

卸载时会保留 Foretoken CRD 和复用的集群组件，并删除平台以及由 CLI 管理的监控或 Gateway 资源。

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

感谢所有为 Foretoken 做出贡献的开发者。

<a href="https://github.com/shiweijiezero/foretoken/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=shiweijiezero/foretoken" alt="Foretoken 贡献者" />
</a>

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 许可证。

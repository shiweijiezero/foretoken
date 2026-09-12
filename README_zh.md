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
| [性能评测](benchmarks/README_zh.md) | 评测模型服务性能 | 开发中 |
| 性能剖析 | PyTorch Profiler 和 Nsight 定位计算、通信及 CPU/GPU 性能瓶颈 | 规划中 |
| 硬件适配 | 统一设备能力、运行时、通信和指标接口；参阅[沐曦部署指南](docs/metax-deployment_zh.md) | 开发中 |
| 请求路由 | 基于负载、队列、KV 复用和服务等级选择实例 | 研究中 |
| 分布式推理 | 聚合部署、Prefill/Decode 分离和 WideEP 并行策略 | 研究中 |
| 控制面 | 模型服务、副本管理、扩缩容、更新和故障恢复 | 开发中 |
| [可观测性](observability/README_zh.md) | 采集服务和加速器指标、评估告警，并通过系统看板查看运行状态 | 开发中 |

## 快速开始

准备好 GPU Kubernetes 集群，并在本机安装 Python 3.11+、`kubectl` 和 Helm。

### 1. 安装命令行工具

```bash
pip install foretoken

# 如果使用源码安装：
# pip install -e .
```

### 2. 安装 Kubernetes 平台

```bash
# 使用 GHCR 发布的镜像：
foretoken install

# 如果使用源码安装：
# foretoken install -e .
```

沐曦 GPU 的部署请参照[沐曦部署指南](docs/metax-deployment_zh.md)。

该命令会在 `foretoken-platform` 命名空间中安装 Foretoken CRD 和控制器，并等待控制器就绪。默认模式通过 `LoadBalancer` 类型的 Kubernetes `Service` 提供前端地址。源码安装会重新构建镜像并更新集群；如果要将当前源码部署到远程集群，请参阅[源码部署指南](docs/custom-deployment_zh.md)。

### 3. 部署快速开始示例

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken

foretoken deploy examples/quickstart --timeout 20m
```

该示例部署一个前端服务、一个 `Qwen/Qwen3-0.6B` 模型副本和一个从 10 GiB 起的运行时缓存 PVC，缓存需要支持扩容的默认 `StorageClass`。工作负载请求 1 张 GPU、8 个 CPU 和 52 GiB 内存；还需为平台预留额外容量。资源配置见[单模型示例](examples/quickstart/README_zh.md)，更多部署配置见 [`examples/`](examples/) 目录。

### 4. 发送测试请求

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### 5. 测量模型服务性能

```bash
pip install 'foretoken[bench]'

# 如果使用源码安装：
# pip install -e '.[bench]'

foretoken bench examples/quickstart --output local,wandb
```

首次上传前运行 `wandb login`；仅需本地结果时使用 `--output local`。更多示例见[模型服务性能评测](benchmarks/README_zh.md)。

## 网关模式

网关模式通过 Kubernetes Gateway 和域名提供统一入口，适合已经使用 Gateway 或需要集中管理外部流量的集群。

在 `examples/quickstart/frontend.yaml` 的 `spec` 中添加访问域名：

```yaml
spec:
  hostname: foretoken.example.com
```

然后运行：

```bash
# 安装平台并启用网关模式
foretoken install --frontend-mode gateway

# 部署快速开始示例
foretoken deploy examples/quickstart --timeout 20m

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

命令会按需安装 Envoy Gateway。复用已有 Gateway 或指定 listener，见[命令行工具使用指南](cli/README_zh.md)。

## 停止与卸载

```bash
# 删除快速开始的资源，包括命名空间和运行时缓存 PVC
foretoken delete examples/quickstart

# 卸载 Foretoken 平台
foretoken uninstall
```

卸载时会保留 Foretoken CRD 和复用的集群组件，并删除平台以及由命令行工具管理的监控或 Gateway 资源。

## 部署指南

- [源码构建与私有镜像仓库](docs/custom-deployment_zh.md)
- [使用 k3d 创建单机 GPU 集群](docs/k3d-deployment_zh.md)
- [沐曦 GPU](docs/metax-deployment_zh.md)

## 相关项目

- [vLLM](https://github.com/vllm-project/vllm)
- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)
- [llm-d](https://github.com/llm-d/llm-d)
- [AIBrix](https://github.com/vllm-project/aibrix)
- [vLLM Production Stack](https://github.com/vllm-project/production-stack)

## 贡献

欢迎通过代码、文档、测试、设计讨论、问题反馈等方式参与 Foretoken。
性能相关变更需要附上测试条件、原始结果和可重复执行的命令。
开发原则、协作约定和 Pull Request 流程见 [《为 Foretoken 做贡献》](CONTRIBUTING_zh.md)。

感谢所有为 Foretoken 做出贡献的开发者。

<a href="https://github.com/shiweijiezero/foretoken/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=shiweijiezero/foretoken" alt="Foretoken 贡献者" />
</a>

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 许可证。

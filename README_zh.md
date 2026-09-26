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
| [评测](benchmarks/README_zh.md) | 评测模型服务性能和模型质量 | 开发中 |
| [性能剖析](benchmarks/docs/profile/README_zh.md) | 采集模型服务的 PyTorch、NVIDIA Nsight Systems 或沐曦 mcTracer 执行时间线 | 开发中 |
| 硬件适配 | 统一设备能力、运行时、通信和指标接口；参阅[沐曦部署指南](docs/metax-deployment_zh.md) | 开发中 |
| 请求路由 | 基于负载、队列、KV 复用和服务等级选择实例 | 研究中 |
| 分布式推理 | 聚合部署、Prefill/Decode 分离和 WideEP 并行策略 | 研究中 |
| 控制面 | 模型服务、副本管理、扩缩容、更新和故障恢复 | 开发中 |
| [可观测性](observability/README_zh.md) | 采集指标、持久保存服务日志、评估告警，并通过系统看板查看运行状态 | 开发中 |

## 快速开始

准备好支持 GPU 和持久存储的 Kubernetes 集群，使用默认存储类（StorageClass）；在本机安装 Python 3.11+、`kubectl` 和 Helm。

### 1. 获取示例并安装命令行工具

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
pip install foretoken

# 从源码目录安装：
# pip install -e .
```

### 2. 安装 Kubernetes 平台

```bash
# 使用 GHCR 发布的镜像：
foretoken install

# 从源码目录构建并安装：
# foretoken install -e .
```

沐曦 GPU 的部署请参照[沐曦部署指南](docs/metax-deployment_zh.md)。

构建工具和远程集群部署见[源码部署指南](docs/custom-deployment_zh.md)。

### 3. 部署快速开始示例

```bash
foretoken deploy examples/quickstart --timeout 20m
```

该示例部署一个前端服务和一个 `Qwen/Qwen3-0.6B` 模型副本，请求 1 张 GPU、8 个 CPU 和 52 GiB 内存。更多部署配置见 [`examples/`](examples/)。

### 4. 发送测试请求

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### 5. 评测与性能剖析

以下示例将结果保存到本地和 W&B。首次使用 W&B 前，执行一次 `wandb login`。

#### 性能：测量延迟和吞吐量

```bash
foretoken perf examples/quickstart --num-prompts 20 --output local,wandb
```

汇总结果展示请求成功率、延迟和吞吐量。其他数据集与负载设置见[性能评测示例](benchmarks/docs/perf/README_zh.md)。

#### 质量：评估回答正确率

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval --tasks gsm8k --limit 100 --output local,wandb
```

该命令对 100 道 GSM8K 数学题评分。EvalScope、任务参数与评分结果的用法见[质量评测](benchmarks/docs/eval/README_zh.md)。

#### 性能剖析：定位执行瓶颈

采集需使用源码安装的 CLI 和平台，见[性能剖析指南](benchmarks/docs/profile/README_zh.md)。快速开始示例已配置持久化采集存储。

```bash
foretoken perf examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local,wandb
foretoken profile view
```

打开打印的网址查看采集结果，按 Ctrl+C 关闭查看器；模型服务继续运行。

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
# 源码安装的平台：
# foretoken install -e . --frontend-mode gateway

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

卸载时会保留 Foretoken CRD、日志存储和复用的集群组件，并删除平台以及由命令行工具管理的监控或 Gateway 资源。

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
  <img src="https://contrib.rocks/image?repo=shiweijiezero/foretoken" width="256" alt="Foretoken 贡献者" />
</a>

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 许可证。

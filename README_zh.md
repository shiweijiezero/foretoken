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

### 1. 安装 Foretoken

通过 Helm 安装 Foretoken：

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --wait
```

### 2. 部署模型服务

`serving_demo.yaml` 声明前端流量入口和模型服务的各项配置：

```bash
FORETOKEN_NAMESPACE=foretoken-demo
FORETOKEN_SERVING_CONFIG=deploy/examples/quickstart/serving_demo.yaml

# 创建模型服务的 namespace；已存在时保持不变
kubectl create namespace "${FORETOKEN_NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -

# 提交模型服务配置，由 K8s Operator 创建并启动相关服务
kubectl apply --server-side \
  --namespace "${FORETOKEN_NAMESPACE}" \
  -f "${FORETOKEN_SERVING_CONFIG}"
```

### 3. 等待服务就绪

```bash
FORETOKEN_NAMESPACE=foretoken-demo
FORETOKEN_SERVING_CONFIG=deploy/examples/quickstart/serving_demo.yaml

# 等待配置文件中的模型服务和流量入口全部就绪
kubectl wait --for=condition=Ready \
  --namespace "${FORETOKEN_NAMESPACE}" \
  --timeout=10m \
  -f "${FORETOKEN_SERVING_CONFIG}"
```

### 4. 发送生成请求进行测试

```bash
curl --fail-with-body --no-buffer \
  https://foretoken.example.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

用户只需提交服务配置；底层资源由 Operator 管理，客户端通过 Gateway 访问模型服务。

## 停止与卸载

```bash
FORETOKEN_NAMESPACE=foretoken-demo
FORETOKEN_SERVING_CONFIG=deploy/examples/quickstart/serving_demo.yaml

# 删除服务配置，停止服务并清理所辖资源：
kubectl delete --wait=true --timeout=10m \
  --namespace "${FORETOKEN_NAMESPACE}" \
  -f "${FORETOKEN_SERVING_CONFIG}"

# 服务资源清理完成后，再卸载 Foretoken：
helm uninstall foretoken \
  --namespace foretoken-platform \
  --wait --timeout 5m
```

## 从源码安装

使用源码目录中的本地 Chart：

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
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

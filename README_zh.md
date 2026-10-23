# Foretoken

[English](README.md) | 简体中文 | [文档](docs/README_zh.md)

Foretoken 是一个 RSI（Recursive Self-Improvement，递归自我改进）友好的生成式推理编排框架。

Foretoken 基于 vLLM、SGLang 等推理引擎，把多个生成实例组织成一套集群服务，负责请求路由、自动扩缩容、实例管理和性能评测。
我们希望将推理集群转化为 Token 工厂，把算力持续转化为满足延迟和质量要求的 Token。
通过统一的部署、评测与性能剖析能力，Foretoken 支持 Agent 持续修改推理系统、验证效果，并依据实验结果开展下一轮优化，形成 [RSI 循环](rsi-playbooks/README_zh.md)。

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
| [RSI](#用-rsi-持续优化推理) | Agent 通过研究、实现与评测持续优化推理系统 | 开发中 |
| [评测](benchmarks/README_zh.md) | 评测模型服务性能和模型质量 | 开发中 |
| [性能剖析](benchmarks/docs/profile/README_zh.md) | 采集模型服务的 PyTorch、NVIDIA Nsight Systems 或沐曦 mcTracer 执行时间线 | 开发中 |
| 硬件适配 | 统一设备能力、运行时、通信和指标接口；参阅[沐曦部署指南](docs/deployment/metax_zh.md) | 开发中 |
| 请求路由 | 基于负载、队列、KV 复用和服务等级选择实例 | 研究中 |
| 分布式推理 | 聚合部署、Prefill/Decode 分离和 WideEP 并行策略 | 研究中 |
| 控制面 | 模型服务、副本管理、扩缩容、更新和故障恢复 | 开发中 |
| [可观测性](observability/README_zh.md) | 采集指标、持久保存服务日志、评估告警，并通过系统看板查看运行状态 | 开发中 |

## 快速开始

先根据集群情况选择部署路径：

| 使用场景 | 指南 |
|---|---|
| 本地单机集群部署 | [k3d 部署指南](docs/deployment/k3d_zh.md) · [kind 部署指南](docs/deployment/kind_zh.md) |
| 使用 K3s、RKE2、KubeSphere、云上或其他 Kubernetes 集群部署 | [Kubernetes 部署指南](docs/deployment/kubernetes_zh.md) |
| 沐曦 GPU 集群部署 | [沐曦部署指南](docs/deployment/metax_zh.md) |

下面以 k3d 部署为例。

### 1. 获取示例并安装命令行工具

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
uv venv --python 3.12 --seed  # 使用 pip 时：python3 -m venv .venv（Python 3.11+）
source .venv/bin/activate
uv pip install -e .  # 或 pip install -e .

# 使用发布的 CLI：
# uv pip install foretoken  # 或 pip install foretoken
```

后续命令在已激活的环境中执行。

### 2. 安装 Kubernetes 平台

创建名为 `foretoken-dev` 的本地 k3d 集群并安装 Foretoken：

```bash
# 使用 nvidia-smi 显示的 GPU 编号 0。若使用两张 GPU，传入 --gpus 0,1。
foretoken cluster create k3d --name foretoken-dev --gpus 0

# 从当前源码构建：
foretoken install -e .

# 使用已发布的平台：
# foretoken install
```

平台在集群中安装一次。各部署实例拥有独立的控制器和服务，默认通过共享 Gateway 提供 HTTP 访问。

宿主机需要 Docker、NVIDIA Container Toolkit、k3d、kubectl 和 [Helm 4](cli/README_zh.md#开始前)，且当前用户可以无 `sudo` 执行 `docker info`。需要时请先按 [k3d 部署指南](docs/deployment/k3d_zh.md) 安装宿主机依赖。使用 kind 或已有 Kubernetes 集群时，按上表进入对应指南。

### 3. 部署快速开始示例

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

不传 `--name` 时，实例名取自 [`frontend.yaml`](examples/quickstart/qwen3-0.6b/frontend.yaml) 中 `FrontendService` 的 `metadata.name`，本例为 `qwen3-0-6b`。

实例包含一个前端服务和一个 `Qwen/Qwen3-0.6B` 模型副本。模型请求 1 张 GPU、4 个 CPU 和 48 GiB 内存，资源上限为 8 个 CPU 和 64 GiB。模型文件和运行时缓存使用平台默认存储，本机示例对应创建集群时准备的 `./data`。更多部署配置见 [`examples/`](examples/)。

同时尝试不同代码改动，见[并行运行候选方案](docs/deployment/source_zh.md#并行运行候选方案)；修改 vLLM，见[使用本地引擎源码](docs/deployment/source_zh.md#修改推理引擎)。

### 4. 发送测试请求

Gateway 根据 Host 将请求路由到目标实例。

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint qwen3-0-6b)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint qwen3-0-6b --host)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### 5. 评测与性能剖析

以下示例将结果保存到本地和 W&B。首次使用 W&B 前，执行一次 `wandb login`。

#### 性能：测量延迟和吞吐量

```bash
foretoken perf examples/quickstart/qwen3-0.6b --num-prompts 20 --output local,wandb
```

汇总结果展示请求成功率、延迟和吞吐量。其他数据集与负载设置见[性能评测示例](benchmarks/docs/perf/README_zh.md)。

#### 质量：评估回答正确率

```bash
foretoken eval examples/quickstart/qwen3-0.6b \
  --evaluator lm-eval --tasks gsm8k --limit 100 --output local,wandb
```

该命令对 100 道 GSM8K 数学题评分。EvalScope、任务参数与评分结果的用法见[质量评测](benchmarks/docs/eval/README_zh.md)。

#### 性能剖析：定位执行瓶颈

快速开始示例已配置持久化采集存储。其他采集工具的准备和用法见[性能剖析指南](benchmarks/docs/profile/README_zh.md)。

```bash
foretoken perf examples/quickstart/qwen3-0.6b \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local,wandb
foretoken profile view
```

打开打印的网址查看采集结果，按 Ctrl+C 关闭查看器；模型服务继续运行。

## 用 RSI 持续优化推理

Agent 可以围绕指定模型、负载和资源预算，研究优化方法、修改代码或配置、部署候选方案，再通过性能评测、质量评测和性能剖析决定下一步。实验报告随迭代更新，记录方案对比、实测收益和保留的改动。

安装并登录 Claude Code 或 Codex 后，在仓库根目录选择一条命令启动 [Qwen3.5-35B-A3B 优化任务](rsi-playbooks/tasks/qwen3.5-35b-a3b_zh.md)。模型、资源、优化目标、时间预算和交付要求统一在任务文件中定义，运行前按实际环境调整。

```bash
# Claude Code
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude \
  '/goal 使用 RSI skill 和 Agent Team，完成 rsi-playbooks/tasks/qwen3.5-35b-a3b_zh.md 中的优化任务。'

# Codex
codex \
  '/goal 使用 RSI skill，完成 rsi-playbooks/tasks/qwen3.5-35b-a3b_zh.md 中的优化任务。'
```

命令会打开交互会话并提交优化任务。暂停时，Claude Code 使用 `/goal clear`，Codex 使用 `/goal pause`，并要求 Agent 停止本任务的实验和采集进程。更详细的目标和约束可写入[任务模板](rsi-playbooks/templates/task-template_zh.md)，在提示词中引用任务文件；完整流程见 [RSI 手册](rsi-playbooks/README_zh.md)。

## 网关模式

复用已有 Gateway 或选择其监听器，见[安装选项](cli/README_zh.md#install)。

使用自定义域名、HTTPS 或限定域名的监听器时，在 `examples/quickstart/qwen3-0.6b/frontend.yaml` 中填写 Gateway 接受的主机名并重新部署。HTTPS 还需对应的 DNS 和网关证书：

```yaml
spec:
  hostname: foretoken.example.com
```

## 停止与卸载

```bash
# 只删除快速开始实例
foretoken delete qwen3-0-6b

# 平台管理员：不再有部署需要平台时卸载
foretoken uninstall
```

卸载时会保留 Foretoken CRD、日志存储和复用的集群组件，并删除平台以及由命令行工具管理的监控或 Gateway 资源。

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

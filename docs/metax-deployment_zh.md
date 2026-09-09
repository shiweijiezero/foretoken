<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 在沐曦 GPU 上部署模型

[English](metax-deployment.md) | 简体中文

在配置好沐曦 GPU 的 Foretoken 集群中，部署模型与使用其他 GPU 的操作相同：准备模型配置，执行 `foretoken deploy`，然后通过 HTTP 调用。Foretoken 负责部署和请求路由，镜像中的沐曦运行环境负责在 GPU 上执行模型；普通部署用户不需要自己安装 vLLM、PyTorch 或 MACA。

本文以 `Qwen/Qwen3-0.6B` 为例。如果集群还没有准备好，由管理员先完成[沐曦平台准备](development/metax-platform_zh.md)，再执行下面的步骤。

## 开始前

准备好 Foretoken CLI、kubectl、curl，以及管理员提供的集群访问配置和 Foretoken 示例源码。命令均在仓库根目录执行。CLI 尚未安装时，按[命令行工具指南](../cli/README_zh.md#安装命令行工具)安装。

和管理员确认两件事：

- 平台已配置沐曦镜像、GPU 和模型缓存存储。默认示例申请 1 张 GPU、8 个 CPU 和 52 GiB 内存。
- 本文使用 `foretoken-demo` namespace 和 Gateway 访问方式。确认可以使用该 namespace，并取得分配给本次模型服务的访问域名。

如果平台使用 `LoadBalancer` 直接访问而不是 Gateway，无需设置下面的 `hostname`；部署和调用步骤不变。

## 1. 部署示例模型

示例配置已经包含模型、缓存和前端服务，无需添加底层 vLLM 启动命令。

在 `examples/quickstart/frontend.yaml` 已有的 `spec` 中加入 `hostname`，将示例域名替换为管理员分配的域名，保留其余配置：

```yaml
spec:
  hostname: foretoken.example.com
```

然后部署：

```bash
foretoken deploy examples/quickstart
```

Foretoken 会准备模型缓存并启动服务，命令在当前配置 Ready 后退出。首次运行需要下载模型，耗时取决于网络和存储。

若要使用其他模型，修改 `examples/quickstart/model.yaml`；资源和缓存配置说明见[单模型示例](../examples/quickstart/README_zh.md)。在共享集群中使用其他 namespace 时，需要同时调整示例的 `namespace.yaml` 和 `kustomization.yaml`，不能只修改本机 kubectl 默认 namespace。

## 2. 发送请求

取得服务地址和 HTTP Host。Host 用于让 Gateway 将请求送到正确的服务：

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

回答会逐段返回，最后出现 `data: [DONE]`。这表示请求已通过 Foretoken 完成，而不只是模型容器启动成功。

修改过模型时，请求中的 `model` 也要相应修改。可以查询当前服务提供的模型名称：

```bash
curl --fail-with-body "$FORETOKEN_FRONTEND_URL/v1/models" \
  -H "Host: $FORETOKEN_REQUEST_HOST"
```

## 3. 查看状态或删除示例

```bash
foretoken status examples/quickstart
kubectl get pods --namespace foretoken-demo
```

使用完后，删除同一份配置创建的资源：

```bash
foretoken delete examples/quickstart
```

该示例包含 namespace，删除时也会删除其中的资源和示例缓存。只在本示例独占的 namespace 中使用这条清理命令，不要用它清理共享 namespace。平台本身由管理员维护，不需要每次部署都安装或卸载。

## 请求未成功时

- **部署一直等待或 Pod 为 Pending：** 查看 `kubectl describe pod --namespace foretoken-demo <pod-name>`。GPU、CPU、内存不足或缓存卷无法绑定时，将具体事件交给管理员处理。
- **返回 404：** 检查配置中的 `hostname` 和请求的 Host 是否一致；`model_not_found` 则表示模型名称不匹配，使用 `/v1/models` 查询。
- **返回 503：** 先查看 `foretoken status` 和 Pod 日志，确认模型已加载、服务已 Ready，再检查访问入口。

本文的沐曦源码环境已验证文本生成和 JSON 约束输出，不用于音频推理。镜像版本、源码安装和底层依赖由[平台准备指南](development/metax-platform_zh.md)说明。

<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 从自定义源码部署 Foretoken

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

本指南面向需要将修改后的 Foretoken 源码构建并部署到 Kubernetes 的开发者。唯一的部署入口是 `make dev-deploy`。

如需了解 k3d cluster 创建和 GPU 前置条件，请参阅 [使用 k3d 部署 Foretoken](k3d-deployment_zh.md)。

## 部署

在仓库根目录执行命令。

首次部署到 k3d 时，下面的命令会使用 GPU 1 创建 `foretoken-qwen-test`，构建镜像并部署 Quick Start：

```bash
CLUSTER=foretoken-qwen-test GPU_INDICES=1 make dev-deploy
```

当前 Kubernetes context 已是 k3d cluster 时，直接部署：

```bash
make dev-deploy
```

标准 Kubernetes cluster 使用可被集群拉取的 OCI registry：

```bash
REGISTRY=registry.example.com/foretoken make dev-deploy
```

使用 private registry 时，先执行 `docker login`，在平台和 workload namespace 中创建同名镜像拉取 Secret，并通过 `IMAGE_PULL_SECRET` 传入其名称。

## 命令行为

`make dev-deploy` 使用 BuildKit 构建 control-plane、frontend 和 model-server 镜像。BuildKit 会在构建输入未变化时复用缓存。脚本比较每个镜像构建前后的本地 image ID；不会检查 Git 变更。

- 使用 k3d 时，脚本只导入本地 image ID 发生变化的镜像。因此，大型 model-server 镜像只有在构建结果变化时才会被导入。
- 设置 `REGISTRY` 时，脚本只推送发生变化的镜像。每个推送的镜像都会获得 deployment tag，Helm upgrade 会自动使用该 tag。model-server 镜像也只有在构建结果变化时才会被推送。
- 脚本会升级本地 Helm Chart、滚动更新发生变化的组件、apply Quick Start，并等待 frontend 和 model service 进入 Ready。
- 在本地 frontend 模式下设置 `DEV_SMOKE=true` 时，Quick Start 就绪后脚本会发送真实的 OpenAI API 兼容格式 chat completion 请求。

## 可选变量

| 变量 | 作用 |
| --- | --- |
| `CLUSTER` | 目标 k3d cluster。cluster 不存在时，必须同时设置 `GPU_INDICES` 以创建它。未设置时会使用当前已有的 `k3d-*` context。 |
| `GPU_INDICES` | 创建 k3d cluster 时向其暴露的宿主机 GPU index，以逗号分隔。 |
| `REGISTRY` | 标准 Kubernetes 部署使用的 OCI repository prefix，例如 `registry.example.com/foretoken`。 |
| `FRONTEND_MODE` | frontend mode：默认 `local`，或 `gateway`。gateway mode 会创建 Chart 的 gateway 配置。 |
| `FRONTEND_HOSTNAME` | `FRONTEND_MODE=gateway` 时写入默认 Quick Start 的对外域名。 |
| `INFERENCE_ENGINE_IMAGE` | 构建 model-server 所用的 inference-engine 基础镜像。源码需要不同的兼容 runtime image 时设置该变量。 |
| `IMAGE_PULL_SECRET` | control-plane 和 workload namespace 使用的镜像拉取 Secret 名称。 |
| `DEV_EXAMPLE` | Chart upgrade 后 apply 的 Kustomize 路径。默认 `examples/quickstart`，该路径同时执行内置 readiness wait；设为空可跳过 example。 |
| `DEV_TIMEOUT` | Helm、rollout 和 readiness 的超时时间，默认 `15m`。 |
| `DEV_SMOKE` | 设为 `true` 可在本地模式部署后发送真实请求，默认 `false`。 |
| `TAG` | 本地开发镜像 tag，默认 `latest`；需要保留多组本地镜像时可使用其他值。 |

例如，部署后发送一次本地模式真实请求：

```bash
DEV_SMOKE=true make dev-deploy
```

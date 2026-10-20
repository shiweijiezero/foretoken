<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 在沐曦 GPU 上部署模型

[English](metax.md) | 简体中文

在配置好沐曦 GPU 的 Foretoken 集群中部署模型，并通过 OpenAI 兼容的 HTTP 接口调用。

本文以 `Qwen/Qwen3-0.6B` 为例。如果集群还没有准备好，由管理员先完成[沐曦平台准备](metax-platform_zh.md)，再执行下面的步骤。

## 开始前

准备好 Foretoken CLI、kubectl、curl，以及管理员提供的集群访问配置和 Foretoken 示例源码。命令均在仓库根目录执行。CLI 尚未安装时，按[命令行工具指南](../../cli/README_zh.md#安装命令行工具)安装。

和管理员确认两件事：

- 平台已配置沐曦镜像、GPU 和模型缓存存储。默认示例申请 1 张 GPU、8 个 CPU 和 52 GiB 内存。
- kubeconfig 已选择有服务操作权限的命名空间。

下面的命令同时适用于 LoadBalancer 直连和 HTTP Gateway。Gateway 会自动为各实例分配路由主机名，CLI 负责解析请求所需的 Host。

## 1. 部署示例模型

示例默认使用仓库的 `data/` 目录，管理员准备好目录访问后即可直接部署。集群使用其他存储时，先按[模型存储指南](../features/model-storage_zh.md)调整 `examples/quickstart/qwen3-0.6b/cache.yaml`。自定义域名、HTTPS 或限定主机名的监听器需要设置[匹配的 hostname](../../README_zh.md#网关模式)。

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

命令在模型和前端服务 Ready 后退出。

不传 `--name` 时，实例名取自 [`frontend.yaml`](../../examples/quickstart/qwen3-0.6b/frontend.yaml) 中 `FrontendService` 的 `metadata.name`，本例为 `qwen3-0-6b`。添加 `--name candidate-a` 可指定另一个实例名，后续命令也改用 `candidate-a`。若要更换模型，修改 `examples/quickstart/qwen3-0.6b/model.yaml`；资源和缓存配置见[单模型示例](../../examples/quickstart/qwen3-0.6b/README_zh.md)。

## 2. 发送请求

取得服务地址和 HTTP Host。Host 用于让 Gateway 将请求送到正确的服务：

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint qwen3-0-6b)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint qwen3-0-6b --host)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

回答会逐段返回，最后出现 `data: [DONE]`。

修改过模型时，请求中的 `model` 也要相应修改。可以查询当前服务提供的模型名称：

```bash
curl --fail-with-body "$FORETOKEN_FRONTEND_URL/v1/models" \
  -H "Host: $FORETOKEN_REQUEST_HOST"
```

需要查看 CPU/GPU 执行时间线时，按[性能剖析指南](../../benchmarks/docs/profile/README_zh.md)采集，工具选择 `--profile-engine pytorch`。

## 3. 查看状态或删除示例

```bash
foretoken status qwen3-0-6b
foretoken status qwen3-0-6b --watch
```

使用完后，删除同一份配置创建的资源：

```bash
foretoken delete qwen3-0-6b
```

删除只移除该实例及其控制器，等待工作负载退出后释放模型缓存引用，不影响相邻实例、Kubernetes 命名空间或管理员提供的存储。缓存回收与自备文件的保留规则见[模型存储](../features/model-storage_zh.md#暂停删除与复用)，共享平台仍由管理员维护。

## 请求未成功时

- 部署一直等待或 Pod 为 Pending： 在 `foretoken status qwen3-0-6b` 中找到对应 Pod，再用 `kubectl describe pod` 加上该 Pod 名称查看事件。GPU、CPU、内存不足或缓存卷无法绑定时，将具体事件交给管理员处理。
- 返回 404： 使用 `foretoken endpoint qwen3-0-6b --host` 返回的 Host；`model_not_found` 则表示模型名称不匹配，使用 `/v1/models` 查询。
- 返回 503： 先查看 `foretoken status` 和 Pod 日志，确认模型已加载、服务已 Ready，再检查访问入口。

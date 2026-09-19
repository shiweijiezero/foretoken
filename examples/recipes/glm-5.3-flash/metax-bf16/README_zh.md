<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# GLM-5.3-Flash BF16 · 沐曦 C500

[English](README.md) | 简体中文

通过 Foretoken 在两台各有 8 张 C500 64 GiB 的节点上运行一个模型执行组：attention TP8×DP2、专家 EP16、1,048,576 token 上下文和原生 MTP（5 个推测 token）。前端采用 KV-aware 路由；两个内存客户端向同一 Mooncake Store 各提供 1 TiB，不使用 SSD offload。

此配方仍在端到端验证中，尚未完成 GLM 生成验收。需要支持 GLM-5.3-Flash、MTP 和混合注意力缓存的源码 MetaX vLLM 运行时，不能直接使用当前发布版的默认推理镜像。

## 准备

平台安装与自定义运行时镜像使用[沐曦平台指南](../../../../docs/development/metax-platform_zh.md)。节点需要发布 GPU 和共享 RDMA 资源，并允许运行时锁定 RDMA 所需内存。

在部署前完成三个环境选择：

- 将 `cache.yaml` 的 `directory` 改为两节点都可访问的共享数据根目录。将完整 `zai-org/GLM-5.3-Flash-BF16` checkpoint 放到该目录下的 `models/zai-org/GLM-5.3-Flash-BF16/`；BF16 权重约 599 GiB。目录权限和其他来源见[模型存储](../../../../docs/model-storage_zh.md)。
- 按[共享 KV 存储示例](../../../shared-kv-store/README_zh.md)构建并向节点提供 Mooncake Store 镜像，将 `kvservice.yaml` 的两处 `image` 设为实际镜像地址。
- 默认使用平台托管的 `rdma/foretoken_rdma`。若平台复用了其他 RDMA 资源，将 `kvservice.yaml` 的 `rdmaResourceName` 改为该资源。

配置合计请求 16 张 GPU、76 核 CPU 和 3208 GiB 主机内存；每个模型成员请求 8 张卡和 512 GiB 内存。资源值是部署起点，不是吞吐承诺；按实际节点容量调整。

## 部署与调用

从仓库根目录执行：

```bash
RECIPE=examples/recipes/glm-5.3-flash/metax-bf16
foretoken deploy "$RECIPE" --timeout 70m
FRONTEND_URL="$(foretoken endpoint "$RECIPE")"

curl --fail-with-body --no-buffer \
  "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"zai-org/GLM-5.3-Flash-BF16","messages":[{"role":"user","content":"你好，请介绍一下你自己。"}],"max_tokens":512,"stream":true}'
```

本例使用平台的直接 LoadBalancer 入口；网关模式的域名与 Host 设置见[沐曦部署指南](../../../../docs/metax-deployment_zh.md)。

修改配置后重新运行同一条 `foretoken deploy`。查看或删除本例：

```bash
foretoken status "$RECIPE"
foretoken delete "$RECIPE"
```

删除会回收该命名空间中的模型、前端和内存 Store；共享目录内的 checkpoint 保留，内存 KV 不保留。

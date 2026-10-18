<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Foretoken 文档

[English](README.md) | 简体中文

首次使用先按[快速开始](../README_zh.md#快速开始)部署模型并发送请求。选择部署环境、配置服务或参与开发时，可从下面的指南进入。

## 部署 Foretoken

| 要完成的任务 | 指南 |
| --- | --- |
| 创建单机 GPU 集群 | [k3d](deployment/k3d_zh.md) |
| 创建本地 CPU 开发集群 | [kind](deployment/kind_zh.md) |
| 使用已有 Kubernetes 集群 | [Kubernetes](deployment/kubernetes_zh.md) |
| 准备沐曦集群并运行模型 | [平台准备](deployment/metax-platform_zh.md) · [模型部署](deployment/metax_zh.md) |
| 构建并部署本地源码改动 | [源码部署](deployment/source_zh.md) |

## 配置与使用服务

| 要完成的任务 | 指南 |
| --- | --- |
| 选择模型来源或本地模型文件 | [模型来源](features/model-sources_zh.md) |
| 配置模型持久存储 | [模型存储](features/model-storage_zh.md) |
| 设置引擎参数、节点、并行方式和并发上限 | [推理参数](features/inference-parameters_zh.md) |
| 调用模型 API、配置前端准入 | [前端](../data-plane/frontend/README_zh.md) |
| 通过命令行管理服务 | [CLI](../cli/README_zh.md) |
| 测量性能或评估模型质量 | [评测](../benchmarks/README_zh.md) |
| 查看指标、日志和告警 | [可观测性](../observability/README_zh.md) |

可运行的配置集中在 [examples](../examples/) 中。

## 开发与维护 Foretoken

先阅读[贡献指南](../CONTRIBUTING_zh.md)和[代码风格](development/code-style_zh.md)。

- 验证改动时，遵循[测试规范](development/testing_zh.md)和 [CI 规范](development/ci_zh.md)。
- 修改部署或存储行为时，保持[源码部署](development/source-deployment_zh.md)和[运行时缓存](development/runtime-cache_zh.md)说明的职责与生命周期约定。
- 选择版本、构建和发布产物，按[发布指南](development/release_zh.md)执行。

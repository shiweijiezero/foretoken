<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Foretoken 文档

[English](README.md) | 简体中文

Foretoken 在 Kubernetes 上编排生成式推理服务，并支持 RSI（递归自我改进）：Agent 研究推理系统、修改代码或配置，再依据实测结果持续优化。

## 快速开始

| 想完成什么 | 从这里开始 |
| --- | --- |
| 部署模型服务并发送请求 | [部署快速开始](../README_zh.md#快速开始) |
| 让 Agent 研究并改进推理系统 | [启动 RSI 任务](../rsi-playbooks/README_zh.md#开始优化任务) |

## RSI 持续优化

在任务中明确模型、负载、优化目标、可用资源和允许的改动范围。Agent 研究方案、实现并部署候选，通过性能与质量评测检验效果，结合性能剖析和实验结果决定下一轮。最终交付保留的改动、测量结果及分析报告。

| 要完成的任务 | 指南 |
| --- | --- |
| 定义目标、资源和交付要求 | [任务模板](../rsi-playbooks/templates/task-template_zh.md) |
| 从已有任务开始 | [优化任务](../rsi-playbooks/tasks/README_zh.md) |
| 研究机制并设计改进方案 | [研究方法与技术知识](../rsi-playbooks/guidance/README_zh.md) |
| 保存实验记录、比较结果并维护报告 | [实验记录手册](../rsi-playbooks/experiment-records_zh.md) |

## 用户指南

### 安装与部署

按模型服务的运行环境选择指南：

| 环境或任务 | 指南 |
| --- | --- |
| 单机 GPU 集群 | [k3d](deployment/k3d_zh.md) |
| 本地 CPU 开发集群 | [kind](deployment/kind_zh.md) |
| 已有 Kubernetes 集群 | [Kubernetes](deployment/kubernetes_zh.md) |
| 沐曦 GPU | [平台准备](deployment/metax-platform_zh.md) · [模型部署](deployment/metax_zh.md) |
| 创建和管理具名部署实例 | [CLI](../cli/README_zh.md#服务部署) |
| 构建和部署源码改动、并行运行候选方案 | [源码部署](deployment/source_zh.md) |

### 模型配置

- [模型来源](features/model-sources_zh.md)：选择 Hugging Face、ModelScope 或本地模型文件。
- [模型存储](features/model-storage_zh.md)：配置持久缓存、共享目录和节点间分发。
- [推理参数](features/inference-parameters_zh.md)：设置引擎参数、节点、并行方式和并发上限。

可运行的部署配置集中在 [examples](../examples/) 中，具体模型的配置见[模型配方](../examples/recipes/README_zh.md)。

### 监控与运维

通过[可观测性指南](../observability/README_zh.md)查看指标、查询持久日志，并配置告警和通知。

## 评测与性能剖析

独立评测服务和 RSI 实验共用同一套测量工具。服务选择与结果保存见[评测指南](../benchmarks/README_zh.md)。

| 想测什么 | 指南 |
| --- | --- |
| 指定负载下的延迟、吞吐量与容量 | [性能评测](../benchmarks/docs/perf/README_zh.md) |
| 回答质量、候选模型与参考模型的差异 | [质量评测](../benchmarks/docs/eval/README_zh.md) |
| CPU/GPU 执行瓶颈 | [性能剖析](../benchmarks/docs/profile/README_zh.md) |
| 多组负载和参数下的表现 | [实验命令与结果对比](../benchmarks/docs/recipes_zh.md) |

## 参考

- [CLI](../cli/README_zh.md)：支持的命令、用途和使用示例。
- [前端 API](../data-plane/frontend/README_zh.md)：模型请求、支持的接口和准入设置。
- [性能指标](../benchmarks/docs/metrics_zh.md)：指标定义与单位。
- [Helm values](../deploy/charts/foretoken/values.yaml)：安装与运行时配置。

<a id="开发与维护-foretoken"></a>

## 开发者指南

先阅读[贡献指南](../CONTRIBUTING_zh.md)和[代码风格](development/code-style_zh.md)。

- 修改指南或导航时，[预览和构建文档](development/documentation_zh.md)。
- 按[测试规范](development/testing_zh.md)和 [CI 规范](development/ci_zh.md)验证改动。
- 维护部署与存储行为时，参阅[源码部署](development/source-deployment_zh.md)和[运行时缓存](development/runtime-cache_zh.md)。
- 构建和发布版本，按[发布指南](development/release_zh.md)执行。

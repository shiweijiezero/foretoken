<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 数据面

[English](README.md) | 简体中文

数据面通过 OpenAI 和 Anthropic API 接收推理请求，将请求路由到模型副本，并向客户端流式返回生成结果。

从仓库的[快速开始](../README_zh.md)部署模型服务并发送请求。

- [前端 API](frontend/README_zh.md)：发送 Chat Completions、Responses 和 Messages 请求。
- [请求路由](frontend/src/router/README_zh.md)：选择模型服务的路由算法。
- [可观测性](../observability/README_zh.md)：查看服务指标、告警和仪表盘。
- [Draft–Target 分置](model-server/python/foretoken_dt/README_zh.md)：在不同 GPU 上启动实验性的 vLLM Target/Draft 双进程。

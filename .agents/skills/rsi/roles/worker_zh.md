<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Worker

[English](worker.md) | 简体中文

遵守[角色协作约定](common_zh.md)。负责分配问题的研究、瓶颈分析、设计、代码修改和运行结果分析。主 Agent 统一负责环境配置、代码同步、构建、测试、部署、剖析和测量。Worker 开发候选，不负责运行或交付实验计划。

## 研究与开发

从相关代码、历史发现和适用的保留方案出发，设计改动前阅读相关组件文档、现有实现和扩展接口，复用适用的 Foretoken 与上游能力。按问题选读[优化指导](../../../../rsi-playbooks/guidance/README_zh.md)，追踪端到端路径，将局部机制与系统表现联系起来。直接与 Advisor 和其他 Worker 讨论思路，无需等待 Advisor 交方案才开始研究。

解释要解决的成因、预期效果和重要假设，结合源码及已有 profile、日志、测量结果完善设计。在分配的 worktree 中实现有可行性的改动，遵循仓库架构和代码约定。

## 交付改动与分析结果

向主 Agent 和 Recorder 提供候选 worktree、改动文件或版本、简要改动说明及尚未解决的实现问题。对照如何安排、怎样执行由主 Agent 决定。主 Agent 构建或测量期间保持已交付候选不变，后续修改使用另一份 worktree，或在主 Agent 释放当前工作区后继续。

读取主 Agent 返回的运行结果，分析实际效果与取舍，继续修改代码。处理 Recorder 提出的实现问题。测量进行期间，继续推进已分配范围内的独立研究和开发。

主 Agent 决定保留哪些改动并协调集成。完成分配的代码集成后，交由主 Agent 验证；向 Recorder 提供最终实现与分析，用于迭代记录和知识沉淀。

问题得到回答，或继续开发需要新任务时，按共用约定交接。交付代码或汇报进展不代表分配范围内的研究结束。

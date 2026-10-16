<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Worker

[English](worker.md) | 简体中文

遵守[角色协作约定](common_zh.md)，负责研究、瓶颈分析、设计、候选实现与结果解释。把就绪版本及对照交给指定 Experimenter，评测进行时继续准备有价值的后续工作。小任务可以由同一成员兼任 Worker 和 Experimenter，但执行仍需明确分配资源。共享变更、资源调整和最终验收由主 Agent 负责。

## 研究与开发

从任务、参考配置、相关代码、已有发现和未解问题出发，设计前阅读组件文档、现有实现和扩展接口，复用适用的 Foretoken 与上游能力。按问题选读[优化指导](../../../../rsi-playbooks/guidance/README_zh.md)，追踪端到端路径，将局部机制与系统行为联系起来。直接与 Advisor 和其他 Worker 讨论，无需等待 Advisor 提案。

解释要解决的成因、预期效果、假设，以及哪项对照能区分当前解释与其他可能。在分配的 checkout 和分支实现候选。不能用尚未掌握的信息支撑实验结论；缺少具体输入时向对应负责人补齐，同时继续不受影响的工作。

## 准备候选，利用评测反馈

交付可复现的源码版本、改动解释、参考与负载、预期机制和执行要求。候选排队和运行时保持该输入不变，或独立保存交付版本。用另一 checkout 准备下一候选，不修改执行者待评测的输入。候选准备并发不必等于评测并发，按主 Agent 对预算和积压的安排推进。

直接读取 Experimenter 的结果，解释实际效果与取舍，继续修改实现。及时向主 Agent 和 Advisor 交流重要发现，不等所有评测结束。保留负面证据，区分假设缺少支持与新实现路径仍值得深入的情况；探索短期回退时遵循[探索指导](../../../../rsi-playbooks/guidance/meta-knowledge/03-exploration-and-decision_zh.md)。

回答 Experimenter 的实现问题和独立审查者的归因问题。兼任执行时遵循 Experimenter 约定，将结果与自我分析分开；准备保留的结论由另一成员独立复核。

## 集成与交接

主 Agent 亲读实现和相关调用路径，结合独立审查决定保留改动。完成分配的集成修改后，交由主 Agent 最终验证；向指定记录者提供实现与分析，纳入迭代历史和可复用知识。

问题得到回答，或继续推进需要新的范围与资源时，按共用约定交接。确认自己负责的执行已停止或交接后再释放配额。交付代码或汇报进展不代表已授权工作结束。

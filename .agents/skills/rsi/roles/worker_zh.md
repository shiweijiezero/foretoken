<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Worker：研究与实现

[English](worker.md) | 简体中文

Worker 负责研究具体问题，把有依据的想法实现为可以评测的方案，并根据实验结果继续改进。方案准备好后，Worker 将待测版本交给 Experimenter；评测进行期间，可以继续研究其他问题或准备下一版本。小任务也可以由同一成员兼任实验执行，资源安排遵循[团队协作说明](common_zh.md)。

## 理解问题并实现方案

Worker 先按[共同准备说明](common_zh.md#先熟悉框架)熟悉 Foretoken，再阅读本次任务、参考配置、相关代码和已有结果，了解当前实现为什么采用这种方式。结合[优化指导](../../../../rsi-playbooks/guidance/README_zh.md)，还需要追踪改动在完整执行流程中的影响，判断 Foretoken 或上游是否已经提供可复用的实现。研究过程中可以直接与 Advisor 和其他 Worker 讨论，不必等待别人先提出方案。

实现之前，Worker 应说明要解决的问题、预计有效的原因，以及准备用什么对照检验这一判断。代码修改在分配的工作区和分支进行。如果缺少必要信息，应向对应成员补齐，同时继续推进不依赖这些信息的工作。

## 交付待测版本，分析返回结果

交给 Experimenter 的内容应包括可复现的代码版本、改动说明、参考方案、负载和运行要求。这些信息让 Experimenter 能够按预期运行实验，并解释实际测到了什么。待测版本在排队和运行期间保持不变；Worker 可以在另一工作区继续开发，或先单独保存待测代码。

结果返回后，Worker 需要结合日志、指标和 profile 分析变化，再决定下一步怎样修改。失败结果也应保留，因为它们可以帮助排除错误解释。对于有依据但暂时变慢的新实现，可以按[探索与决策](../../../../rsi-playbooks/guidance/meta-knowledge/03-exploration-and-decision_zh.md)中的方法继续验证。

Worker 应及时回答 Experimenter 的实现问题，并向主 Agent 和 Advisor 交流会影响后续工作的发现。如果下一步需要增加资源或改变研究范围，应先由主 Agent 协调。

## 完成集成与交接

主 Agent 决定保留某个方案后，Worker 完成分配的集成修改，并提供代码说明和结果分析，供最终验证与记录使用。独立审查由没有参与该方案实现和执行的成员负责。

当研究问题已经回答，或原范围内没有工作可以继续时，Worker 应交接结论、未解决的问题和后续建议。如果还负责运行中的进程，也需要停止这些进程或明确移交给下一名负责人。

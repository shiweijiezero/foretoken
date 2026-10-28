<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 在 Claude Code 中执行 RSI

[English](SKILL.md) | 简体中文

先阅读本目录下的 `../../../.agents/skills/rsi/SKILL_zh.md`，按共用流程和角色说明执行。

使用交互式 Agent Team。创建成员前确认 Teams 已启用；未启用时，按[启动说明](../../../rsi-playbooks/README_zh.md#claude-code)打开专用会话。SDK 或非交互会话中的命名子 Agent 不是 Team 成员。

用户在主会话设置一个 `/goal`，涵盖任务结果、团队推进和交付，成员不需要分别设置目标。共享 Task 工具可用时，用它记录负责人和依赖。

按共用的[接续流程](../../../.agents/skills/rsi/continuation_zh.md)使用 `--harness claude`。只为持续研究和执行的成员登记原生 ID 或 teammate name；项目 `TeammateIdle` hook 可以将下一步交给同一成员，无需另建队友。

并行写入使用任务分支和独立工作区，在任务中给出绝对路径。创建 Team 成员时，不在 Agent 调用中传 `isolation`，该选项会创建普通子 Agent；工作区单独准备。

恢复会话后，先核对原预算、实验记录、作业和成员状态，再补建缺失成员。暂停或停止时，通知成员停止本任务的作业并保存结果；`/goal clear` 只取消目标，不会自动停止实验进程。

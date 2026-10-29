<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 在 Claude Code 中执行 RSI

[English](SKILL.md) | 简体中文

先阅读本目录下的 `../../../.agents/skills/rsi/SKILL_zh.md`，按共用流程和角色说明执行。

使用交互式 Agent Team。创建成员前确认 Teams 已启用；未启用时，按[启动说明](../../../rsi-playbooks/README_zh.md#claude-code)打开专用会话。SDK 或非交互会话中的命名子 Agent 不是 Team 成员。

项目 `.claude/settings.json` 已启用 Claude Code 的 Ultracode 动态工作流。Ultracode 决定实质任务何时需要生成工作流，Agent Team 负责长期成员、直接通信和唤醒；任务中指定的模型与 effort 仍然有效。

用户在初始会话设置一个 `/goal`，确定任务结果和预算，成员自行推进日常工作与交付，不另设目标。原生 Teams 提供成员身份、直接通信和唤醒，共享 RSI 工作池负责工作归属和依赖，不在原生 Task 工具再维护第二份归属或依赖记录。

按共用的[接续流程](../../../.agents/skills/rsi/continuation_zh.md)使用 `--harness claude`。只为持续研究和执行的成员登记原生 ID 或 teammate name；项目 `TeammateIdle` hook 让成员继续所持工作或领取就绪工作，无需另建队友。

并行写入使用任务分支和独立工作区，在任务中给出绝对路径。创建 Team 成员时，不在 Agent 调用中传 `isolation`，该选项会创建普通子 Agent；工作区单独准备。

恢复会话后，先核对原预算、实验记录、作业和成员状态，再补建缺失成员。暂停或停止时，通知成员停止本任务的作业并保存结果；`/goal clear` 只取消目标，不会自动停止实验进程。

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 在 Claude Code 中执行 RSI

[English](SKILL.md) | 简体中文

阅读本文件所在目录下的 `../../../.agents/skills/rsi/SKILL_zh.md`，遵循共用流程与角色约定。使用交互式 Agent Team，由用户设置主 Agent 的单个 `/goal`。

创建成员前，确认当前交互会话支持原生 Team。Agent Teams 是默认关闭的实验功能；SDK 或非交互会话中的命名子 Agent 仍是普通子 Agent。不可用时，按[手册中的专用会话说明](../../../rsi-playbooks/README_zh.md#claude-code)启动。
用户在主会话中设置 `/goal <完成条件>`，把执行期间维持有效任务与工作人数、回应完成或阻塞后的新任务请求、达到任务结束条件时交付结果，都纳入同一个 goal。成员保留角色上下文并接受具体任务，无需各自设置 `/goal`。主 Agent 构思方向、派工、综合决策，统一负责环境准备、部署与并行实验和最终验收。Worker 研究开发候选并分析返回结果，执行边界与持续协作遵循共用角色约定。

并行候选由主 Agent 先创建 Git worktree，再在派工中提供绝对路径。Worker 修改分配的候选，主 Agent 从交付版的对应工作区执行。运行期间不改源码，继续开发时使用另一 worktree，或等待主 Agent 释放原工作区。Claude Code 的 Agent 启动调用若显式传入 `isolation`，会创建普通子 Agent 而非 teammate，因此将工作区准备与 Team 成员创建分开。

任务权限与交接遵循共用角色约定。阶段结果用于继续推进；任务完成或需要改变范围时，由主 Agent 协调。共享 Task 工具可用时，可用它记录负责人和依赖。按任务选择合适的监测与持续执行工具；有后台工作运行时，`/goal` 可能暂缓完成判定。

恢复任务时，根据原记录和预算核对仍在运行的成员，补建缺失成员后再分配未完成工作。恢复主会话的 goal 不会自动恢复 in-process teammates。用户暂停、停止及任务截止时间优先于继续派工，由主 Agent 协调资源收尾与交付。用户可用 `/goal clear` 取消 goal；取消本身不会停止实验进程。

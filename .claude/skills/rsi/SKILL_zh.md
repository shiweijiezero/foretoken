<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 在 Claude Code 中执行 RSI

[English](SKILL.md) | 简体中文

阅读本文件所在目录下的 `../../../.agents/skills/rsi/SKILL_zh.md`，遵循共用流程与角色约定。使用交互式 Agent Team，由用户设置主 Agent 的单个 `/goal`。

创建成员前，确认当前交互会话支持原生 Team。Agent Teams 是默认关闭的实验功能；SDK 或非交互会话中的命名子 Agent 仍是普通子 Agent。不可用时，按[手册中的专用会话说明](../../../rsi-playbooks/README_zh.md#claude-code)启动。
用户在主会话中设置 `/goal <完成条件>`，把执行期间维持有效任务与工作人数、回应完成或阻塞后的新任务请求、达到任务结束条件时交付结果，都纳入同一个 goal。成员保留角色上下文并接受具体任务，无需各自设置 `/goal`。主 Agent 构思方向、分配资源与执行负责人、协调共享变更并负责最终验收；委派执行和持续协作遵循共用角色约定。

使用任务分支，为并行写入者提供独立 checkout，在派工中给出绝对路径和归属；不要求 worktree 隔离会话。执行期间保持候选源码不变。创建 teammate 时将工作区准备单独完成：Agent 启动调用显式传入 `isolation` 会创建普通子 Agent，而非 teammate。

任务权限与交接遵循共用角色约定。阶段结果用于继续推进；任务完成或需要改变范围时，由主 Agent 协调。共享 Task 工具可用时，可用它记录负责人和依赖。按任务选择合适的监测与持续执行工具；有后台工作运行时，`/goal` 可能暂缓完成判定。

恢复任务时，根据原记录和预算核对仍在运行的成员，补建缺失成员后再分配未完成工作。恢复主会话的 goal 不会自动恢复 in-process teammates。用户暂停、停止及任务截止时间优先于继续派工，由主 Agent 协调资源收尾与交付。用户可用 `/goal clear` 取消 goal；取消本身不会停止实验进程。

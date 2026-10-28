<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 接续 RSI 工作

[English](continuation.md) | 简体中文

项目 hook 帮助成员接着推进已分配的研究问题，并提醒主 Agent 安排后续工作。它只作用于明确启用的 RSI 会话，未绑定的会话不受影响。一个主会话对应一个 RSI 任务，其他任务使用新的主会话。

## 启用本次任务

使用 Python 3.11+ 和工作区中的 hook 配置。Codex 读取 `.codex/hooks.json`：桌面应用通过“设置 → Hooks”审阅并信任项目定义，终端 CLI 使用 `/hooks`。Claude Code 读取项目的 `.claude/settings.json`。两者都无需在用户全局配置中安装 hook，具体机制见 [Codex](https://learn.chatgpt.com/docs/hooks) 和 [Claude Code](https://code.claude.com/docs/en/hooks) 官方说明。

使用 Codex 关联 worktree 时，将 hook 配置放在主 checkout，各任务工作区保留接续脚本。缺少脚本的旧工作区会跳过这些 hook。在 Hooks 设置页或 CLI 的 `/hooks` 中核对项目来源路径，确认实际加载的定义。

主 Agent 确定任务和原始截止时间后启用接续。将 `RSI_HARNESS` 设为 `codex` 或 `claude`，`RSI_SESSION` 设为原生主会话 ID，`RSI_TASK` 设为任务文件路径，`RSI_DEADLINE` 设为带 UTC 偏移的 ISO 截止时间，在任务工作区执行：

```bash
python -I .agents/skills/rsi/scripts/continuation.py activate \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --task "$RSI_TASK" --deadline "$RSI_DEADLINE"
```

没有时间预算的任务省略 `--deadline`。暂停的任务可再次启用，保留原截止时间；已完成任务不再恢复。返回的状态表示 hook 绑定，不是运行环境中的实际活跃人数。

## 安排成员的下一步

例如，Worker 正在实现候选，交付后可以在测量期间继续研究调用链中尚未解释的部分。成员仍在运行时，用 `queue` 登记这项后续工作。将 `RSI_AGENT` 设为成员的原生 ID，`RSI_ROLE` 设为 `explorer`、`worker` 或 `experimenter`，`RSI_NEXT_STEP` 写明下一问题和相关资料路径：

```bash
python -I .agents/skills/rsi/scripts/continuation.py queue \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --role "$RSI_ROLE" --next-step "$RSI_NEXT_STEP"
```

使用运行环境返回的成员原生 ID。如果 Codex 只返回任务名，成员可在首次进度更新中附上自己的 `CODEX_THREAD_ID`。登记该成员时，`RSI_SESSION` 仍使用主 Agent 的 ID。Claude 队友也可使用明确的 teammate name。Observer 和一次性审查成员不登记，完成后正常结束。

`SubagentStop` 发生时，hook 取出已登记的下一步，让同一成员继续；Claude 的 `TeammateIdle` 使用同一份待办。每条指令只交付一次，之后结束时不会重复。

成员完成后没有待交付步骤时，主 Agent 在下一次工具返回（`PostToolUse`）后收到提醒；若先结束回合，则由 `Stop` 提醒。此前的完成事件合并为一次提醒。主 Agent 读取证据、安排下一项工作并更新记录；Observer 完成后正常登记并处理发现。

使用相同的 harness 和主会话 ID 调用 `status`，可查看尚待交付的指令和已登记的决定。它反映 harness 状态，不代表原生活动：恢复的成员可能仍显示 `waiting`，下一次完成仍会通知主 Agent。`queue` 只登记后续工作，不会启动或恢复成员；空闲成员应通过原生 follow-up 工具恢复。

## 等待、暂停与结束

后续确实依赖尚未返回的结果时，将成员设为 `waiting`；研究问题已回答时设为 `done`。`RSI_REASON` 写明结论，或缺少的证据和恢复条件：

```bash
python -I .agents/skills/rsi/scripts/continuation.py release \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --state waiting --reason "$RSI_REASON"
```

主 Agent 通过原生工具恢复成员并交付其他有价值的问题，或说明该角色暂缺的原因。有更后一步的工作时，再用 `queue` 登记。

任务暂停或同一聊天转做普通工作前，省略 `--agent`，用 `--state paused` 解除主会话的接续；任务完成时使用 `--state complete`。待交付指令会清空，实验和采集器仍按 RSI 流程清理。到期或会话结束后，hook 也不再继续工作；Codex 的 `Interrupt` 会暂停绑定。恢复时重新启用同一任务，通过原生工具恢复成员，再按需登记后续工作。

绑定统一保存在仓库的 Git common directory 中，关联 worktree 共用，按 harness 和主会话隔离；实验记录仍保存在原位置。团队分工中保留主会话和成员身份，恢复后的主 Agent 即可管理原绑定。

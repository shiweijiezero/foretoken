<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 接续 RSI 工作

[English](continuation.md) | 简体中文

原生成员通过 `scripts/continuation.py` 共享任务内工作，领取就绪问题或有信息价值的阶段，直接交付证据，不需要派工 Agent。完整说明与原始产物保留在现有实验笔记。

## 启用本次任务

使用 Python 3.11+ 与项目中已信任的 hook。Codex 从 `.codex/hooks.json` 加载定义，在 Settings → Hooks 或 `/hooks` 中检查；Claude Code 加载 `.claude/settings.json`。Codex linked worktree 的 hook 配置放在主工作区，脚本保留在各任务工作区，并核对运行环境实际加载的定义。

初始化时，将 `RSI_HARNESS` 设为 `codex` 或 `claude`，`RSI_SESSION` 设为任务的原生 root ID，`RSI_TASK` 指向任务文件，`RSI_DEADLINE` 使用带时区偏移的原始 ISO 截止时间。在任务工作区运行：

```bash
python3 -I .agents/skills/rsi/scripts/continuation.py activate \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --task "$RSI_TASK" --deadline "$RSI_DEADLINE"
```

没有时间预算的任务才省略截止时间。恢复时再次启用同一任务并保持原截止时间，已经完成的任务不能重开。新任务自动记录起始时间；接手没有起始时间的旧绑定时，先用 `--started-at` 提供原始 ISO 起始时间，再配置巡查。共享 SQLite 文件位于仓库的 Git common directory，按 harness 和任务会话隔离；它记录工作责任，不代替 GPU 分配或原生成员活动。

## 登记并领取工作

持续成员将 `RSI_AGENT` 设为自己的原生 ID，登记所承担的角色。例如 Worker 运行：

```bash
python3 -I .agents/skills/rsi/scripts/continuation.py register \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --role worker \
  --thread-id "$NATIVE_THREAD_ID" \
  --responsibility "$RSI_RESPONSIBILITY"
python3 -I .agents/skills/rsi/scripts/continuation.py claim \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --wait 30
```

角色为 `explorer`、`worker` 或 `experimenter`。持续成员领取研究、实现和执行工作；独立采用审查与周期巡查由每轮全新的 Evaluator 完成。缺少专职执行角色时，才用可重复的 `--accept` 参数明确兼任。每轮新建的 Evaluator 不登记为持续成员。

有可用容量时再领取。领取以原子操作选择满足条件的就绪工作，复用课题与环境上下文，同时让旧工作先于后续新工作获得机会。未完成依赖和审查排除条件会阻止领取，不把工作预留给忙碌成员。`--wait` 在工具内等待，短暂检查之间释放 SQLite。

没有合适工作时，所有持续成员都立即收到 `action: research`，即使设置了 `--wait` 也不等待别人发布问题；应从当前采用配置和任务未解问题继续研究，Worker 和 Experimenter 可以自行发布下一实现或测量阶段。空领取不意味着研究职责结束。

## 安排成员的下一步

完整说明写入已有迭代笔记。将 `RSI_WORK` 设为不重复的工作名，`RSI_BRIEF` 指向已有说明，`RSI_TOPIC` 和 `RSI_ENVIRONMENT` 分别表示课题和可复用环境，再发布下一问题或阶段：

```bash
python3 -I .agents/skills/rsi/scripts/continuation.py submit \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --work "$RSI_WORK" --kind experimenter \
  --description "Measure the candidate described in $RSI_BRIEF" \
  --topic "$RSI_TOPIC" --environment "$RSI_ENVIRONMENT"
```

工作类型为 `explorer`、`worker`、`experimenter`、`review` 或 `evaluation`。`review` 仅为兼容旧记录保留；新的独立采用审查使用 fresh `evaluation` 派发。课题与环境只表达可复用的上下文，不指定独占执行者。可重复的 `--after WORK` 引用已存在、需要先完成的工作；领取者会拿到这些产物路径，读取其中的实际发现，包括失败实验。提交新阶段前先读取该 scope 当前采用末端，在已有说明中写入实际父版本；方案被采用后由 scope 负责人先更新末端，再领取下一累计阶段。

不要把新的 review 工作分配给持续成员。在 Evaluator 的 fresh assessment 中链接实现与执行原件；Evaluator 读取原始证据并记录适用范围和未解问题。`--after`／`--exclude` 仅为兼容已有 review 记录保留。

完成时运行 `finish --agent "$RSI_AGENT" --work "$RSI_WORK" --result "$RSI_RESULT"`，并传入相同 harness/session。产物必须是已有文件或分析，其中写清实际结果。有用证据到达就发布后续工作，不等整套矩阵；已知的空闲使用者需要恢复时用原生 follow-up。共享工作领取与原生 Agent 派发是两项操作。

`submit`、`finish`、`handoff` 和旧 `queue` 的响应会在存在熟悉且可用的成员或其他空闲合格成员时返回简短的 `notifications` 路由。产出者通过原生 follow-up 或成员间直接消息发送提醒；提醒只发给这些对象，不广播给全体。没有发送提醒时，成员仍可领取就绪工作。

用 `handoff --agent "$RSI_AGENT" --work "$RSI_WORK" --reason "$RSI_REASON"` 加相同 harness/session，将同一工作交回供其他成员领取。原因注明原运行作业、产物和下一恢复动作，接手者先检查原件，不把交接当新实验。原生成员已停止时，同级成员可用 `handoff --from-agent EXPECTED_OWNER` 恢复工作，在原因中记录停止证据和原作业位置；只有旧负责人仍持有该项时才改变归属，随后按正常流程领取就绪工作。`status` 可查看工作、责任人与结果。旧 `queue` 接口在共享池中替换该成员尚未领取的下一步，运行中和已交接的恢复工作保持不变；待办不为指定成员保留。

## 周期巡查

传入 harness/session，用 `schedule --interval 3600 --count 1` 配置每小时一次新巡查；具体间隔和人数采用任务要求，人数为 0 时取消尚未领取的巡查，已经运行的评估由原负责人收尾。重新设置保持原计时起点。领取操作会使到期的 `evaluation` 派发工作可用，一批尚未完成时不堆积错过的轮次。

空闲成员领取派发工作，只向全新 Evaluator 提供任务、角色和工作目录，接收其评估路径。实际评估保存后，再用它完成派发工作，不能用启动通知代替。Evaluator 将可执行发现直接交给相关负责人；工作池不产生常设 Evaluator 调度者或报告作者，下面的 Agent Scheduler 只负责唤醒持续成员 thread。

工作池排期是唯一的评估时间表，存活成员的 idle hook 或 claim 会生成到期派发项，再由可用成员消费；它不是后台 daemon，运行环境退出后不会自行派发。不通过提醒另建第二套评估排期。

## 运行独立 Agent Scheduler

Hook 和 `queue` 只持久化工作，不会唤醒 idle 的原生 thread。任务需要在 Lead 回合结束后继续时，运行一个连接 Codex app-server 的独立 Agent Scheduler：

```bash
CODEX_APP_SERVER_SOCKET=/path/to/app-server-control.sock \
python3 -I .agents/skills/rsi/scripts/agent_scheduler.py \
  --harness codex --session "$RSI_SESSION" --cwd "$PWD" --interval 2
```

每名成员登记稳定的 `--thread-id` 和完整的 `--responsibility`。Scheduler 读取 `thread/read` 状态，在 SQLite 中领取 `pending` responsibility，调用 `thread/resume`，只有 `turn/start` 返回 turn ID 后才标记为 `running`。thread 已 active 时绝不重复启动；完成或回到 idle 后重新进入 `pending`。Scheduler 崩溃由 lease 过期恢复。进程请求存在不确定失败时按至少一次投递处理，因此每次 prompt 带 dispatch ID，成员必须先检查已有工作再行动。

绑定的 session 进入 paused、complete 或 expired 时 Scheduler 停止；到达任务截止也必须显式停止。`SessionEnd` 只保存持久状态，不暂停 RSI，也不生成新工作。成员只能以 `waiting`、`done`、`paused`、`expired` 或明确失败／资源冲突释放；`idle` 和 `needs_assignment` 是调度故障，不是正常研究状态。

## 等待、暂停与结束

没有就绪工作能解除真实依赖时，运行 `release --agent "$RSI_AGENT" --state waiting --reason "$RSI_REASON"`，并传入 harness/session。释放成员前先完成或交接所持工作，职责结束时使用 `done`。结果到达后由生产者通过原生 follow-up 恢复已知的等待成员。

成员结束 hook 会让未完成职责的成员继续所持工作或领取就绪工作。Codex 使用 `SubagentStop`，Claude 使用 `TeammateIdle`。Codex 的 `Stop` 和 `PostToolUse` 保留为 root fallback，也会生成到期 Evaluator 派发，因此漏掉某个成员停止事件时，另一个存活事件可以补上。被 guard 的成员停止会写入 root notice，不会静默丢掉 active 工作；下一次 root `PostToolUse` 以非阻塞上下文提示，root `Stop` 只消费一次 notice 并请求一轮检查。`SessionEnd` 只保留持久状态，不生成新工作，因为主线程正在结束。这不意味着原生 Agent 能脱离运行环境存活。

用户要求暂停或任务完成时，用 `release --state paused` 或 `release --state complete`，传入 harness/session 和原因，不传成员 ID。root Interrupt 暂停任务，原始截止时间阻止新领取。各负责人仍需停止自己的服务、构建和采集作业并确认退出。恢复时先检查原作业，启用同一任务，再用原生工具恢复成员。

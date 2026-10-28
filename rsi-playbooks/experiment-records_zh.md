<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 实验记录手册

[English](experiment-records.md) | 简体中文

## 选择实验命令

根据优化目标，从[实验命令参考（recipes）](../benchmarks/docs/recipes_zh.md)中选择合适的命令，并按待测模型和实验假设调整负载与参数。

例如：

| 想测什么 | 选择 recipes 中的哪一组 |
| --- | --- |
| 不同输入输出长度、并发下的性能 | [输入输出长度与并发](../benchmarks/docs/recipes_zh.md#输入输出长度与并发) |
| 输入变长对性能的影响 | [长上下文性能](../benchmarks/docs/recipes_zh.md#长上下文性能) |
| 多轮对话负载下的表现 | [多轮对话：每秒启动多少段](../benchmarks/docs/recipes_zh.md#多轮对话每秒启动多少段) |
| 满足延迟目标时能承载多少流量 | [SLO 达标率与 goodput](../benchmarks/docs/recipes_zh.md#slo-达标率与-goodput) |
| 按真实请求时间分布测量 | [真实时间戳回放](../benchmarks/docs/recipes_zh.md#真实时间戳回放) |

## 保存实验记录

在 recipe 命令中调整以下参数：

| 参数 | 填写方式 |
| --- | --- |
| `--output` | 用逗号组合：`experiment` 保存实验记录（含本地结果），`plot` 导出图表，`wandb` 上传结果，`quiet` 将终端进度和汇总改为写入日志，错误仍显示，`raw` 保存文本 HTTP 性能评测的实际请求与响应。 |
| `--output-dir` | Agent 自定实验根目录，建议 `results/<优化目标>/<实验名称>`；同一实验的不同方案共用目录。 |
| `--iteration` | Agent 自定方案名。同一方案补跑沿用名称，换方案时更名，每次运行生成独立记录；省略时自动创建编号迭代。 |

已登录 W&B 时，推荐 `experiment,wandb,plot`，方便用户与 Agent 查看和比较结果；未登录时使用 `experiment,plot`，保存本地记录和图表。需要检查实际输入与回答时再加 `raw`。

例如，在已登录 W&B 的情况下，将 recipe 的输出参数改为：

```bash
--output experiment,wandb,plot \
--output-dir results/decode/parallelism \
--iteration tp2
```

## 找到实验记录

实验目录由 `--output-dir` 指定，方案目录由 `--iteration` 命名：

```text
results/decode/parallelism/
├── report.md                  # Agent 维护的整体报告
├── figures/
│   ├── progress.yaml          # 一个指标、一组对照条件的绘图数据
│   ├── progress-milestones.png
│   └── progress-search.png
├── team/                      # Agent 维护的团队与巡查记录
│   ├── README.md              # 任务、提醒与处理决定
│   └── observations/          # 每轮 Observer 的独立巡查笔记
└── iterations/tp2/
    ├── notes/
    │   ├── iteration.md       # 摘要、文档链接与决定
    │   ├── plan.md            # 示例：完整方案与执行计划
    │   └── analysis.md        # 示例：详细结果分析
    └── runs/perf-000001/
        ├── generated/
        │   └── context.json
        └── artifacts/
            └── <具体结果目录>/
                ├── config.json
                ├── environment.json
                ├── metrics.json
                └── …
```

记录保存在执行评测命令的机器上，相对路径以该命令的工作目录为起点。在远端运行时，到远端按命令打印的路径查看。参数扫描的一次运行可以包含多个结果目录；使用 W&B 时，也可通过输出的链接查看曲线和比较结果。

### 通过文档共享方案与代码

每个方案的完整说明、计划和分析统一写入 `<output-dir>/iterations/<name>/notes/`。例如，`plan.md` 充分说明问题与依据、方案和备选方法、关键取舍、实现与验证计划，`analysis.md` 展开实测结果的解释。讨论方案时即可创建这些文档，后续运行实验时用同一名称指定 `--iteration`。

`iteration.md` 用摘要表介绍各项内容，并链接完整文件，表格之后保留注明时间的审查与决定。`report.md` 作为整体入口，链接各迭代笔记，汇总目标、方案对比和跨迭代结论。

主 Agent 提供全体成员可读取的实验目录，注明所在主机和绝对路径。共享文件系统上的不同 checkout 使用同一绝对 `--output-dir`，通过 `--iteration` 区分候选；跨主机没有共享存储时，在报告中链接实际记录位置及访问方式。

代码保留在各自负责的 checkout 或 worktree 中。方案文档注明负责人、代码所在主机与绝对路径、分支和版本，链接相关文件或已保存的 diff，并说明未提交的改动。

按[团队沟通约定](../.agents/skills/rsi/roles/common_zh.md#直接交流减少等待)，简短协调直接发消息，长说明或多人需要阅读的内容放进共享文件。消息给出摘要和准确路径，后续只补充变化，不必重述全部背景。尚未决定的问题直接与负责人讨论，决定记回笔记，等待答复时继续其他工作。每份文档由一名作者维护，较长的审查意见或替代方案由提出者另写文件。

例如，说明吞吐提升时，给出参考方案和候选方案的 `metrics.json` 路径及对比字段；说明错误时，给出日志路径及相关行号或时间范围。代码结论注明版本、文件和函数，涉及 Agent 决策或工具调用的结论则指向会话记录及具体消息或时间范围。发送结论时附上这些原始证据位置，接收者先读取相关部分，需要更多上下文时再展开。

研究资料、脚本、准备日志和清理记录也放在本轮 `notes/` 下，按需要组织；`plan.md`、`analysis.md` 只是文件名示例。评测命令会补建缺失的迭代笔记，保留已有内容，报告和其他文件由成员维护。原始运行产物仍保存在 `runs/`。删除被引用的工作区前，先保存所需代码并更新链接；清理时保留仍被引用的证据。

## 保存团队与会话记录

主 Agent 在 `<output-dir>/team/README.md` 维护巡查安排，链接 `team/observations/` 下的每轮笔记。行动的负责人、决定和预期结果保存在已有任务记录中；未解决的问题持续链接首次记录，并在原处更新进展，报告引用这份当前状态。巡查笔记保留各次检查的依据和判断。

### 读取会话证据

从任务文件和实验目录查找主 Agent 与成员会话，结合工作目录、时间、session ID 和父子关系确认记录范围。Observer 自行定位并只读检查，无需成员重新汇报进度。

有只读历史接口时，读取完整条目和工具输出，例如 [Codex 会话历史](https://learn.chatgpt.com/docs/app-server)或 [Claude 会话接口](https://code.claude.com/docs/en/agent-sdk/sessions)。原始文件通常位于：

- Claude Code：`~/.claude/projects/`，包括会话的 `subagents/` 目录。
- Codex：`$CODEX_HOME/sessions/`，`CODEX_HOME` 默认是 `~/.codex`。

直接读取 JSONL 时，[会话读取工具](../.agents/skills/rsi/scripts/session_records.py)提供分页索引，并将关联证据一起返回。它需要 Python 3.11+，无需额外安装包。将 `RSI_SESSION` 设为文件路径，`RSI_SINCE` 设为带 UTC 偏移的 ISO 起始时间，在仓库根目录执行：

```bash
python -I .agents/skills/rsi/scripts/session_records.py index "$RSI_SESSION" --since "$RSI_SINCE"
```

索引包含 Codex 的开始、完成和中断事件，用于核对成员随时间变化的工作状态。默认省略已识别的继承记录并显示数量；需要这些背景时，加上 `--include-inherited`。后续页用 `--after-line` 传入返回的 `next_after_line`。选择源文件行号，设置 `RSI_LINE` 后读取证据：

```bash
python -I .agents/skills/rsi/scripts/session_records.py show "$RSI_SESSION" "$RSI_LINE"
```

`show` 一起返回选中记录、配对的工具输入与结果，以及这段区间内的命令记录，每条都保留来源位置。同一区间内出现并不证明命令由该调用触发。输出较长时，将 `next_offset` 传给 `--offset`，并在各续页中通过 `--through-line` 保持首次返回的 `through_line`，避免会话追加内容导致分页错位。需要新记录时，不带这些选项开始一次新的读取。

工具会标明可识别的继承历史、缺失回执、不可读内容和已有截断标记。会话缺少必要细节时，继续读取保存的输出或产物文件，否则将事实保留为待核实。索引预览用于定位，不能替代证据本身。

## 分析结果

下表列出常用实验文件。以上例的 `results/decode/parallelism/iterations/tp2/runs/perf-000001/` 为起点，`generated/` 下保存命令与源码记录，`config.json` 等评测文件位于 `artifacts/<具体结果目录>/`。根据实验类型和输出选项，还会产生预热记录、GPU 分配、Prometheus 观测、评测框架报告等文件，按分析需要查看。

| 顺序 | 读取内容 | 分析重点 |
| --- | --- | --- |
| 1 | `generated/context.json` | 命令是否完成、退出状态及本次源码记录 |
| 2 | `config.json`、`environment.json` | 比较负载、生成参数、客户端版本与服务配置。使用 Foretoken 部署时，还可查看记录的服务变化和代码版本 |
| 3 | `metrics.json`、结果图表 | 成功数、实际输出长度与目标指标如何变化，是否支持本轮假设 |
| 4 | `raw_output.json`；启用 `raw` 时的 `responses.jsonl` | 定位异常请求，以 `request_id` 对照指标、停止原因与实际回答；质量评测查看 `native/` 中的原生报告和已保存的逐题记录 |
| 5 | 启用 `quiet` 时的 `generated/run.log` 和结果目录中的 `run.log`；质量评测的 `evaluator.log` | 定位客户端准备与执行中的错误；结合已有资源观测和 profile 解释性能差异，确定下一步补测。模型加载或引擎错误查看对应服务端日志。 |

按[绘图示例](../benchmarks/docs/recipes_zh.md#绘制与对照结果)指定 base，比较已保存的性能结果。结合生成的数值、重复统计、条件差异及上表中的原始记录，判断假设是否成立、是否需要补测。

## 完善实验说明

将执行证据、判断过程和当前摘要分开保存：

| 记录 | 更新方式 |
| --- | --- |
| 已完成的运行产物 | 保留原始命令、指标、响应和日志，后续分析引用它们。运行中的文件仍由执行程序维护。 |
| `iterations/<name>/notes/iteration.md` | 指定 Experimenter 维护摘要与文档索引，追加运行结论、独立审查、决策和更正，注明时间、来源及证据链接。 |
| `iterations/<name>/notes/` 下的其他文件 | 各作者维护完整方案、计划、分析或复核，由迭代笔记链接。 |
| `report.md` | 指定作者更新整体对比、保留方案和未解问题，链接对应迭代条目和决定。 |

分析、审查和决定分别注明作者，每项决定写清行动和适用范围。更正结论或改变决定时，链接旧条目与新依据，保留认识变化的过程。

每次运行后，追加运行目录或 W&B 链接、测试的假设、已有对照产物的链接及证据支持的结论，也保留失败和中断原因。按[迭代模板](templates/iteration-template_zh.md)记录历史。成员把补充内容交给指定作者，避免并行覆盖记录。

报告中的当前结论与迭代历史保持一致，并链接支撑条目。可复用知识也可随认识变化修订，保留指向实验历史的链接。

## 持续维护用户报告

实验开始时，由主 Agent 或指定作者按[报告模板](templates/report-template_zh.md)创建 `report.md`，并向用户提供文件路径。报告放在任务指定的交付位置；未指定时放在实验根目录。报告链接任务、实验目录和各轮迭代笔记，链接以报告的实际位置为起点。恢复实验时继续更新同一份文件。报告及其[进展图数据和图片](templates/progress-charts_zh.md)由同一作者维护，评测命令负责保存原始证据。

每轮测量结束后（包括失败尝试），以及分析改变结论或保留方案时，及时更新报告。按模板在总结内列出对照表，随后放进展图，再在同一份报告中逐方案说明动机、实际实现和结果。表格与正文使用一致、能说明改动的名称，并相互关联。完整命令、日志、重复测量和决策历史保存在迭代笔记中。

面向未参与实验的读者，区分已有证据支持的结论、待验证假设和未完成验证。尚无测量结果时，说明目标、起始条件和未测项目。

每次更新报告，都同步更新绘图数据、重新生成并查看两张图。

暂停、停止或完成时，更新同一份报告，写清原因、已取得的结果、保留改动和未完成验证，将过时安排改为实际状态，并再次提供报告路径。

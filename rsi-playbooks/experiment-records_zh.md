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
├── report.md                  # 链接生成结果的任务总览
├── figures/
│   └── progress/
│       ├── progress-summary.md     # 生成的比较与图表
│       ├── progress-summary.csv
│       └── progress-runs.csv
├── team/                      # Agent 维护的团队与巡查记录
│   ├── README.md              # 任务、提醒与处理决定
│   └── evaluations/          # 每轮 Evaluator 的独立评估笔记
└── iterations/tp2/
    ├── notes/
    │   ├── iteration.md       # 作者分析、文档链接与决定
    │   ├── progress.yaml      # 运行引用与限定范围的判断
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

每个方案的问题、假设、结果和决定保存在 `<output-dir>/iterations/<name>/notes/iteration.md`。简短实验只需这一份笔记及原始结果链接；补测时用 `--iteration` 沿用方案名。

方案、分析较长或由其他成员独立撰写审查时，再单独成文并从 `iteration.md` 链接。笔记保留注明时间的发现与决定；`report.md` 说明任务目标，链接生成的跨方案对比和各作者记录。

主 Agent 提供全体成员可读取的实验目录，注明所在主机和绝对路径。共享文件系统上的不同 checkout 使用同一绝对 `--output-dir`，通过 `--iteration` 区分候选；跨主机没有共享存储时，在报告中链接实际记录位置及访问方式。

代码保留在各自负责的 checkout 或 worktree 中。方案文档注明负责人、代码所在主机与绝对路径、分支和版本，链接相关文件或已保存的 diff，并说明未提交的改动。

按[团队沟通约定](../.agents/skills/rsi/roles/common_zh.md#直接交流减少等待)，简短协调直接发消息，长说明或多人需要阅读的内容放进共享文件。消息给出摘要和准确路径，后续只补充变化，不必重述全部背景。尚未决定的问题直接与负责人讨论，决定记回笔记，等待答复时继续其他工作。每份文档由一名作者维护，较长的审查意见或替代方案由提出者另写文件。

例如，说明吞吐提升时，给出参考方案和候选方案的 `metrics.json` 路径及对比字段；说明错误时，给出日志路径及相关行号或时间范围。代码结论注明版本、文件和函数，涉及 Agent 决策或工具调用的结论则指向会话记录及具体消息或时间范围。发送结论时附上这些原始证据位置，接收者先读取相关部分，需要更多上下文时再展开。

研究资料、脚本、准备日志和清理记录也放在本轮 `notes/` 下，按需要组织；`plan.md`、`analysis.md` 只是文件名示例。评测命令会补建缺失的迭代笔记，保留已有内容，各作者维护自己的分析与进展记录。原始运行产物仍保存在 `runs/`。删除被引用的工作区前，先保存所需代码并更新链接；清理时保留仍被引用的证据。

## 保存团队与会话记录

巡查安排保存在 `<output-dir>/team/README.md`，Evaluator 将每轮独立发现写入 `team/evaluations/`。行动的负责人、决定和预期结果保存在已有任务记录中；未解决的问题持续链接首次记录，并在原处更新进展，报告引用这份当前状态。评估笔记保留各次检查的依据和判断。

任务记录保存目标、分工和决定。成员实际活动从原生 harness 读取，hook 绑定从 continuation 查询，作业状态由执行负责人核实。报告需要快照时，注明观察时间和来源，避免在任务、成员和报告文件中各维护一份实时状态。结束跟进时，由负责人链接实际改动的结果，或说明停止行动的决定。

### 读取会话证据

从任务文件和实验目录查找主 Agent 与成员会话，结合工作目录、时间、session ID 和父子关系确认记录范围。Evaluator 自行定位并只读检查，无需成员重新汇报进度。

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
| `iterations/<name>/notes/progress.yaml` | Experimenter 记录所选运行、实测父配置及各适用范围的判断，绘图命令据此生成共同对比。 |
| `report.md` | 面向用户的简洁总览负责任务元数据、状态和链接。由一名指定总览负责人在 `.foretoken-report.lock` 下更新文字；数值比较来自生成的进展摘要。 |

分析、审查和决定分别注明作者，每项决定写清行动和适用范围。更正结论或改变决定时，链接旧条目与新依据，保留认识变化的过程。

每次运行后，Experimenter 在[迭代笔记](templates/iteration-template_zh.md)中补入原件位置与结果，再按[团队交付流程](../.agents/skills/rsi/roles/common_zh.md#随实验返回结果)直接恢复 Worker 和已安排的审查者。例如，诊断命令退出码为 0，但保存的输出比较可能不一致；应同时记录两个事实并链接比较结果。分别说明实测速度、适用的验证证据与是否保留。

正式测量、诊断控制和工具验证均注明用途与样本数，没有 `metrics.json` 的辅助产物也要保留入口。远端运行由 Experimenter 将结果保存到约定位置，包括从临时存储转移原件；接收者直接读取原件，或使用交付的副本。恢复时先检查已登记的远端作业与结果位置。

报告中的当前结论与迭代历史保持一致，并链接支撑条目。可复用知识也可随认识变化修订，保留指向实验历史的链接。

## 持续维护用户报告

任务开始时，按[报告模板](templates/report-template_zh.md)创建 `report.md`，链接任务和结果目录，并向用户提供路径。恢复时沿用同一份报告。指定一名持续成员作为总览负责人；他只在任务级 `.foretoken-report.lock` 下更新元数据、状态、链接和简短方案说明。总览负责人不负责测量或派发，因此不会成为实验瓶颈。

每个实测阶段结束或判断变化后，更新 `iterations/<name>/notes/progress.yaml`，然后执行：

```bash
foretoken plot --progress results/decode/parallelism
```

命令读取所有迭代所选的既有运行和判断，生成 `figures/progress/progress-summary.md`、对应 CSV 表及原生里程碑图、搜索图。生成摘要是数值唯一来源；`report.md` 链接它，不再手工拼接跨 scope 总表。各 scope 顶部列出已测采用分支末端及实际运行／配置／源码记录，便于后续选择适用的父配置；祖先历史仍保留。指标、重复次数、波动和已记录的源码／配置证据均来自原件。机制解释、输出差异与独立复核保留在作者的 `iteration.md` 及分析中。

例如，候选的进展记录可以选择一次完成的命令及其实测父方案：

```yaml
label: + QKV views
measurements:
  random-concurrency-1:
    runs: [../runs/perf-000001]
    parent: ../../baseline/runs/perf-000001
    status: valid
    retained: true
```

路径以本 YAML 所在目录为起点。scope 命名负载及比较条件，不同协议、条件分别记录，同一 scope 只选择可比的重复测量。在用户最初配置的迭代记录中设置 `baseline: true`，后续控制保持具名方案身份。实测速度已取得而适用的输出或质量问题未解决时使用 `reference`；拒绝方案和失败尝试也保留。[进展图指南](templates/progress-charts_zh.md)说明字段与自定义指标选择。

后续实现和探索从相应适用范围内已验证的配置出发，记录候选真正运行的父配置及已有改动。A、B 都从 P 独立测量时，它们是两个分支，采用其中一个不等于证明 A+B。组合配置需要自己的运行与判断。围绕改动边界执行最小必要验证，复用仍适用的证据，不例行重复完整矩阵或成对控制。

每份进展记录由该迭代唯一的 Experimenter 维护，并按[原子发布示例](templates/progress-charts_zh.md#刷新共同视图)完整替换后刷新共同视图。一个进展文件只有一个写者并原子替换；绘图命令沿用图片目录锁，将发现、读取和发布串行完成。若多个进展文件在重绘期间变化，待负责人完成后再运行一次，使摘要对应同一稳定快照。生成表保留实测父方案与原始运行／源码引用；里程碑只连接已记录的实测父节点，独立分支保持分开。采用是明确且限定适用范围的判断，不由最高吞吐自动决定。

交付到文档平台时，发布或链接同一生成摘要，不再维护另一份数值报告。修改总览时，用报告锁包住读取当前文件、生成完整替换和写回的命令；不能把旧副本直接覆盖新报告。暂停、停止或完成时，在任务总览记录原因与剩余工作，链接各执行者真实的资源退出证据。已有人工图表 YAML 仍可用 `foretoken plot --progress CHART.yaml` 绘制。

# RSI Playbooks

[English](README.md) | 简体中文

这套手册帮助 Agent 使用 Foretoken 研究和优化推理系统：修改代码、部署方案、测量效果，再根据结果继续改进。任务文件规定模型、资源、预算和交付要求，[优化指导](guidance/README_zh.md)提供研究方法与已有经验。

## 开始优化任务

先取得仓库中的任务和示例：

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

以下以 [Qwen 吞吐与解码延迟优化](tasks/qwen-decode_zh.md)为例。使用前确认具备任务要求的资源；自己的任务可按[任务模板](templates/task-template_zh.md)编写，并替换指令中的文件路径。

### Claude Code

启动启用了 Agent Teams 的交互会话：

```bash
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude
```

在会话中输入：

```text
/goal 使用 RSI skill 和 Agent Team，完成 rsi-playbooks/tasks/qwen-decode_zh.md 中的优化任务。阅读已有实验记录和相关指导，遵守任务的资源、时间预算及结束条件，交付规定结果。
```

需要暂停时，用 `/goal clear` 结束自动续轮，并要求团队停止本任务的实验和采集进程。

### Codex

在仓库目录打开 Codex，输入：

```text
/goal 使用 RSI skill，完成 rsi-playbooks/tasks/qwen-decode_zh.md 中的优化任务。阅读已有实验记录和相关指导，遵守任务的资源、时间预算及结束条件，交付规定结果。
```

用 `/goal pause` 暂停目标，并要求成员停止实验与采集；用 `/goal resume` 继续。

## Agent 分工

| 角色 | 主要工作 |
| --- | --- |
| 主 Agent | 选择研究方向、安排资源、综合结果，负责最终验证与交付 |
| Advisor | 持续研究和探索新方法，发现问题并反思当前方向 |
| Worker | 研究具体问题、实现方案，根据实验结果继续改进 |
| Experimenter | 构建和评测方案，观察资源使用、记录结果并清理实验环境 |

候选准备与评测可以并行，人数和并发分别按任务资源安排。小任务可以兼任角色，重要结论由未参与对应实现和实验的成员独立检查。

具体的派工、工作区与资源交接方式见 [RSI skill](../.agents/skills/rsi/SKILL_zh.md)。

## 使用步骤

团队先阅读任务、代码和已有结果，选择值得研究的问题，再通过实现和实验验证想法。一个问题可以经历多轮修改，也可能得到“不适用”的结论。主 Agent 根据结果决定继续、换方向或整合方案，并在任务结束时完成验证与交付。

固定负载组合使用现有参数扫描连续运行。性能结果和质量结论分别判断，详细方法见[探索与决策](guidance/meta-knowledge/03-exploration-and-decision_zh.md)。

## 选择评测

| 目的 | 入口 |
| --- | --- |
| 测量延迟和吞吐 | [`foretoken perf`](../benchmarks/docs/perf/README_zh.md) |
| 评估回答质量 | [`foretoken eval`](../benchmarks/docs/eval/README_zh.md) |
| 定位执行瓶颈 | [`foretoken perf --profile`](../benchmarks/docs/profile/README_zh.md) |
| 比较并发、输入长度等条件 | [参数扫描与实验命令](../benchmarks/docs/recipes_zh.md) |

命令选项可用 `foretoken perf --help` 和 `foretoken eval --help` 查询。

## 查看结果

任务会保存每轮的命令、配置、指标和原始结果，并维护实验总览与最终报告。位置由任务文件指定，目录说明见[实验记录手册](experiment-records_zh.md)。使用 W&B 时，记录中也会保留对应运行链接。

更多任务见[优化任务](tasks/README_zh.md)，可复用的技术认识见[知识库](guidance/knowledge/README_zh.md)。

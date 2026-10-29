# RSI 推理优化

[English](README.md) | 简体中文

RSI（Recursive Self-Improvement，递归自我改进）让 Agent 持续研究、实现和验证推理优化。用户通过任务文件指定目标与边界，Agent 团队调用 Foretoken 完成部署、评测和性能剖析，再根据结果继续改进。

## 开始优化任务

### 1. 准备仓库和任务文件

安装并登录 Claude Code 或 Codex，在 Foretoken 仓库中启动任务。尚无仓库时执行：

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

以 [Qwen3.5-35B-A3B NVIDIA 任务](tasks/qwen3.5-35b-a3b-nvidia_zh.md)为起点，其他硬件任务见[已有任务](tasks/README_zh.md)。自定义任务可通过[任务模板](templates/task-template_zh.md)编写，例如保存为 `my_docs/rsi/my-task_zh.md`。`my_docs/` 已被 Git 忽略。

### 2. 修改任务设置

用户需要确认以下内容；不需要限制的选项可交给 Agent 选择，具体填写方式见任务模板。

| 设置 | 需要说明什么 |
| --- | --- |
| 模型与目标 | 优化哪个模型，主要改善吞吐、延迟还是其他指标。 |
| 可用资源 | 允许访问的节点或集群，以及整个实验可用的 GPU、CPU 和内存。 |
| 最终部署预算 | 最终方案允许使用多少资源，与实验期间的总资源分开填写。 |
| 评测与约束 | 负载、并发、精度和质量要求，以及允许修改的代码或配置范围。 |
| 时间与交付 | 工作时长或结束条件、已有实验，以及报告和结果保存位置。 |
| Agent 设置 | Explorer、Worker、Experimenter 的并行人数；每轮 Evaluator 数量与巡查间隔。 |

### 3. 启动 Agent

下面以 NVIDIA 任务为例，使用自定义任务时替换路径。

仓库包含用于[接续 RSI 工作](../.agents/skills/rsi/continuation_zh.md)的项目 hook，需要 Python 3.11+，只对明确启用的 RSI 任务生效。

#### Claude Code

```bash
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude
```

项目设置已启用 Claude Code 的 Ultracode 动态工作流。它与 Agent Team 及 effort 强度分开；任务文件中的模型和 effort 配置仍然有效。

在会话中输入：

```text
/goal 使用 RSI skill 和 Agent Team，完成 rsi-playbooks/tasks/qwen3.5-35b-a3b-nvidia_zh.md 中的优化任务。
```

#### Codex

在 Codex 桌面应用中打开仓库，通过“设置 → Hooks”审阅并信任 Foretoken 项目的 hook。使用终端 CLI 时，在仓库目录运行 `codex`，再输入：

```text
/hooks
```

信任项目 hook 后，在 Codex 会话中启动目标：

```text
/goal 使用 RSI skill，完成 rsi-playbooks/tasks/qwen3.5-35b-a3b-nvidia_zh.md 中的优化任务。
```

如果 Codex 因 Agent 数量上限无法创建所需团队，在用户配置 `~/.codex/config.toml`（设置了 `CODEX_HOME` 时为 `$CODEX_HOME/config.toml`）中调整上限，例如：

```toml
[agents]
max_concurrent_threads_per_session = 16
```

按同时工作的 Explorer、Worker、Experimenter 总数，加上一轮 Evaluator 所需人数预留名额。

## 查看进展

各 Experimenter 记录自己的结果与判定，由现有绘图入口汇总比较和进展。报告将实测收益、采用方案与未解问题链接到原件，记录格式见[实验记录手册](experiment-records_zh.md)。

需要调整目标、预算或改动范围时，在会话中说明即可，由主 Agent 更新任务记录与受影响的工作安排。

暂停任务时，Claude Code 使用 `/goal clear`，Codex 使用 `/goal pause`，同时要求团队停止实验、采集进程和巡查提醒。Codex 可用 `/goal resume` 继续，恢复时沿用原任务和已有结果。

## 架构与角色分工

任务文件表达用户要求；RSI skill 和角色说明组织 Agent 的工作；Foretoken 提供构建、部署、评测与性能剖析能力。实验结果供团队判断下一步，并写入用户报告。

| 角色 | 主要职责 |
| --- | --- |
| 主 Agent | 建立任务、查看记录、向负责人提出纠正，并向用户简要汇报；成员独立推进。 |
| Explorer | 检索本地或在线资料，研究机制，提出方法和新问题。 |
| Worker | 负责实现与结果解释，在任务授权内持续改进或采用方案。 |
| Experimenter | 领取就绪实验、复用环境、保存原始结果并释放资源。 |
| Evaluator | 独立检查变化与开放问题，将可执行发现直接交给相关负责人。 |

## 执行规则与参考资料

| 读者与用途 | 文档入口 |
| --- | --- |
| 用户：编写或调整任务 | [任务模板](templates/task-template_zh.md) · [已有任务](tasks/README_zh.md) |
| Agent：执行流程与角色要求 | [RSI skill](../.agents/skills/rsi/SKILL_zh.md) · [团队协作](../.agents/skills/rsi/roles/common_zh.md) |
| Agent：调研方法与技术经验 | [优化指导](guidance/README_zh.md) · [知识库](guidance/knowledge/README_zh.md) |
| 部署执行者：更新源码与复用环境 | [源码部署](../docs/deployment/source_zh.md) |
| 评测执行者：性能、质量与剖析 | [评测指南](../benchmarks/README_zh.md) · [实验命令](../benchmarks/docs/recipes_zh.md) |
| 用户与 Agent：查看和维护结果 | [实验记录手册](experiment-records_zh.md) |

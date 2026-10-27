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
| Agent 设置 | Explorer、Worker、Experimenter 的并行人数；每轮 Observer 数量与巡查间隔。 |

### 3. 启动 Agent

下面以 NVIDIA 任务为例，使用自定义任务时替换路径。

#### Claude Code

```bash
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude
```

在会话中输入：

```text
/goal 使用 RSI skill 和 Agent Team，完成 rsi-playbooks/tasks/qwen3.5-35b-a3b-nvidia_zh.md 中的优化任务。
```

#### Codex

在仓库目录打开 Codex，输入：

```text
/goal 使用 RSI skill，完成 rsi-playbooks/tasks/qwen3.5-35b-a3b-nvidia_zh.md 中的优化任务。
```

## 查看进展

实验报告 `report.md` 随进展更新，汇总方案对比、性能与质量结果、保留方案和待验证的问题。原始数据和详细分析可从报告中的链接查看，记录格式见[实验记录手册](experiment-records_zh.md)。

需要调整目标、预算或改动范围时，直接在会话中说明，并更新任务文件。

暂停任务时，Claude Code 使用 `/goal clear`，Codex 使用 `/goal pause`，同时要求团队停止实验、采集进程和巡查提醒。Codex 可用 `/goal resume` 继续，恢复时沿用原任务和已有结果。

## 架构与角色分工

任务文件表达用户要求；RSI skill 和角色说明组织 Agent 的工作；Foretoken 提供构建、部署、评测与性能剖析能力。实验结果供团队判断下一步，并写入用户报告。

| 角色 | 主要职责 |
| --- | --- |
| 主 Agent | 选择研究方向、安排资源、处理决策并组织交付。 |
| Explorer | 检索本地或在线资料，研究机制，提出方法和新问题。 |
| Worker | 实现方案，分析实验结果并继续改进。 |
| Experimenter | 准备环境、部署和运行实验、保存结果、释放资源。 |
| Observer | 每轮以新 Agent 独立读记录，逐项检查进度、研究深度和资源使用，提出调整建议。 |

## 执行规则与参考资料

| 读者与用途 | 文档入口 |
| --- | --- |
| 用户：编写或调整任务 | [任务模板](templates/task-template_zh.md) · [已有任务](tasks/README_zh.md) |
| Agent：执行流程与角色要求 | [RSI skill](../.agents/skills/rsi/SKILL_zh.md) · [团队协作](../.agents/skills/rsi/roles/common_zh.md) |
| Agent：调研方法与技术经验 | [优化指导](guidance/README_zh.md) · [知识库](guidance/knowledge/README_zh.md) |
| 部署执行者：更新源码与复用环境 | [源码部署](../docs/deployment/source_zh.md) |
| 评测执行者：性能、质量与剖析 | [评测指南](../benchmarks/README_zh.md) · [实验命令](../benchmarks/docs/recipes_zh.md) |
| 用户与 Agent：查看和维护结果 | [实验记录手册](experiment-records_zh.md) |

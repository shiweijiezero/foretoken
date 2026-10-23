# RSI 推理优化

[English](README.md) | 简体中文

RSI（Recursive Self-Improvement，递归自我改进）让 Agent 持续研究和优化推理系统：研究方案，修改代码或配置，部署并测量效果，再根据结果开展下一轮。Foretoken 为这个过程提供部署、评测和性能剖析工具。任务文件规定模型、资源、预算和交付要求，[优化指导](guidance/README_zh.md)提供研究方法与已有经验。

## 开始优化任务

安装并登录 Claude Code 或 Codex，再取得仓库中的任务和示例：

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

以下以 [Qwen3.5-35B-A3B 优化](tasks/qwen3.5-35b-a3b_zh.md)为例。使用前确认具备任务要求的资源；自己的任务可按[任务模板](templates/task-template_zh.md)编写，并替换指令中的文件路径。

### Claude Code

启动启用了 Agent Teams 的交互会话：

```bash
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude
```

在会话中输入：

```text
/goal 使用 RSI skill 和 Agent Team，完成 rsi-playbooks/tasks/qwen3.5-35b-a3b_zh.md 中的优化任务。阅读已有实验记录和相关指导，遵守任务的资源、时间预算及结束条件，交付规定结果。
```

需要暂停时，用 `/goal clear` 结束自动续轮，并要求团队停止本任务的实验和采集进程。

### Codex

在仓库目录打开 Codex，输入：

```text
/goal 使用 RSI skill，完成 rsi-playbooks/tasks/qwen3.5-35b-a3b_zh.md 中的优化任务。阅读已有实验记录和相关指导，遵守任务的资源、时间预算及结束条件，交付规定结果。
```

用 `/goal pause` 暂停目标，并要求成员停止实验与采集；用 `/goal resume` 继续。

## Agent 分工

主 Agent 统筹整个任务，根据已有结果选择研究方向并安排资源。它负责综合各方发现，决定保留哪些改动，并组织最终验证与交付。

Advisor 持续研究技术机制和优化方法，帮助团队发现新问题、反思当前方向。它既可以深入已有瓶颈，也可以探索尚未进入候选清单的新思路。

Worker 围绕具体问题实现可以评测的代码或配置，再根据实验结果继续改进。

Experimenter 负责构建和评测这些方案，同时观察资源使用、保存结果，并在实验结束后清理环境。

下一方案的准备可以与当前方案的评测并行，团队根据资源和实际耗时分别安排两阶段的并发。

具体的派工、工作区与资源交接方式见 [RSI skill](../.agents/skills/rsi/SKILL_zh.md)。

## 复用环境，持续迭代

每个候选沿用固定的源码目录和实例名，供后续迭代复用。需要修改代码时，先[安装一次源码平台](../docs/deployment/source_zh.md#从源码安装)；已有发布版平台也可通过同一命令更新。只调整参数的实验可直接使用已安装的应用。

| 改动内容 | 更新方式 |
| --- | --- |
| Go、Rust、Python 应用代码 | 执行 `foretoken deploy`，用 `-e` 指定候选源码目录，复用未变化的环境和兼容的编译缓存。 |
| 运行环境依赖或基础镜像 | 用 `-e` 从更新后的源码目录重新部署，重建受影响的环境。 |
| 共享 CRD 或平台配置 | 在源码目录执行 `foretoken install -e .`。 |

每轮修改后，先部署候选并确认请求成功，再评测性能和回答质量，据此决定保留哪些改动、下一轮尝试什么。部署与评测使用同一个 `--name`；`perf`、`eval` 测量已部署的服务，不会替它更新代码。

复用环境和缓存后，后续代码更新通常比首次准备更快。记录时，将首次准备耗时与每轮的构建、发布、启动和评测耗时分开。

[并行候选](../docs/deployment/source_zh.md#并行运行候选方案)通过配置的存储共享模型下载，各自保留独立的运行缓存和结果。迭代间用 `foretoken stop` 释放 GPU 并保留实例，不再需要时再删除。

负载组合确定后，可通过现有参数扫描比较候选；交付前再验证组合后的改动。选择实验和分析结果的方法见[探索与决策](guidance/meta-knowledge/03-exploration-and-decision_zh.md)。

## 选择评测

| 目的 | 入口 |
| --- | --- |
| 测量延迟和吞吐 | [`foretoken perf`](../benchmarks/docs/perf/README_zh.md) |
| 评估回答质量 | [`foretoken eval`](../benchmarks/docs/eval/README_zh.md) |
| 定位执行瓶颈 | [`foretoken perf --profile`](../benchmarks/docs/profile/README_zh.md) |
| 比较并发、输入长度等条件 | [参数扫描与实验命令](../benchmarks/docs/recipes_zh.md) |

命令选项可用 `foretoken perf --help` 和 `foretoken eval --help` 查询。

## 查看结果

实验报告（`report.md`）汇总各方案的性能对比、分析结论和当前采用的方案，并随实验进展更新。详细的实验过程和原始数据可通过报告中的链接查看；记录选项、结果位置和报告维护方法见[实验记录手册](experiment-records_zh.md)。

更多任务见[优化任务](tasks/README_zh.md)，可复用的技术认识见[知识库](guidance/knowledge/README_zh.md)。

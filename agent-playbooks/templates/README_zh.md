# 参考模板

[English](README.md) | 简体中文

[实验目录说明](experiments_zh.md)展示说明文件和运行证据的存放位置。编写 `results/` 中的说明时，可参考以下模板：

- [实验说明](experiment-template_zh.md)：整体目标、范围、比较方法和多轮尝试的结论。
- [迭代说明](iteration-template_zh.md)：一轮方案的分析、设计、改动、测量和决策。

每次 perf/eval 命令结束后，由用户或 Agent 编辑 `results/<goal>/<motivation>/iterations/<name>/notes/iteration.md`：追加本次运行的链接，写明验证了什么、结果说明什么，以及失败原因或仍需测量的问题。同一方案补跑时继续追加到这份说明。

本轮结束后，再更新 `results/<goal>/<motivation>/notes/experiment.md`，汇总本轮结论、链接迭代说明，并写明后续方向。程序保存执行证据并创建空白说明，不会代写这些解释。代码决策与资源清理见[收尾步骤](experiments_zh.md#每轮结束后)。

编写 `guidance/` 中的优化技巧条目时，参考[知识条目模板](guidance-template_zh.md)，说明思路、适用场景、实现线索和资料来源。

# 实验记录

[English](experiments.md) | 简体中文

把同一目标下的方案和测量放在一起，方便后续尝试利用已有发现。实验对应整体目标，迭代对应一轮方案，运行对应一次性能测试或质量评测命令。

## 组织记录

将实验保存在 `results/<优化目标>/<实验动机>/`，用能说明方案的名称标识迭代，例如 `queue-aware-routing`。

```text
results/<goal>/<motivation>/
├── notes/experiment.md
└── iterations/<name>/
    ├── notes/iteration.md
    └── runs/<run>/
        ├── generated/
        └── artifacts/
```

实验说明持续汇总整体结论，迭代说明记录本轮假设、设计、改动和结果解释，可分别参考[实验模板](../templates/experiment-template_zh.md)和[迭代模板](../templates/iteration-template_zh.md)。

通过 `--output experiment`、`--output-dir results/<goal>/<motivation>` 和 `--iteration <name>` 将 perf/eval 命令归入同一轮迭代；重复执行会新增独立运行记录，不覆盖旧结果。

选择该输出方式后，`generated/` 保存采集到的命令、耗时、状态和可获取的源码快照，`artifacts/` 保存评测配置与结果。开发者或 Agent 维护说明，并引用这些记录。在包含改动的源码仓库中执行命令，以捕获对应源码状态；服务代码若单独构建，在说明中另行记录其来源。

## 解释结果

说明测量支持或否定了哪个假设，还有什么未得到解答。比较时考虑负载、精度、缓存状态或硬件差异，链接支撑结论的测量和模型输出。

最后可选地复核实现和原始输出，检查针对测试的取巧实现、模型行为变化，以及收益归因是否有据可依。

## 记录迭代耗时

记录调查、设计、实现、部署、评测和分析所花的时间。有实测值时引用记录，估算值注明来源。区分整条命令耗时与其中的测量窗口，包含关系的时间不重复相加。

根据这些耗时判断下一轮可以复用或简化哪些工作，并只选择能回答当前问题的评测。

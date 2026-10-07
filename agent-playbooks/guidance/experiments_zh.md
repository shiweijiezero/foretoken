# 实验记录

[English](experiments.md) | 简体中文

把同一目标下的方案和测量放在一起，方便后续尝试利用已有发现。实验对应整体目标，迭代对应一轮方案，运行对应一次性能测试或质量评测命令。

## 组织记录

将实验保存在 `results/<优化目标>/<实验动机>/`，用能说明方案的名称标识迭代，例如 `queue-aware-routing`。

```text
results/<goal>/<motivation>/
├── notes/
│   └── experiment.md
└── iterations/
    ├── queue-aware-routing/
    │   ├── notes/
    │   │   └── iteration.md
    │   └── runs/
    │       ├── perf-000001/
    │       │   ├── generated/
    │       │   │   ├── context.json
    │       │   │   ├── changes/
    │       │   │   └── run.log
    │       │   └── artifacts/
    │       │       └── <result-directory>/
    │       │           ├── config.json
    │       │           ├── environment.json
    │       │           ├── metrics.json
    │       │           └── ...
    │       ├── eval-000001/
    │       │   ├── generated/
    │       │   └── artifacts/
    │       └── perf-000002/
    │           ├── generated/
    │           └── artifacts/
    └── another-approach/
        ├── notes/
        │   └── iteration.md
        └── runs/
```

上例为 `--output experiment` 的目录布局。`context.json` 记录命令、状态、耗时和源码信息；`changes/` 在可采集源码时保存改动文件，保持仓库相对路径。`run.log` 在启用 `quiet` 时保存。`artifacts/` 内保留评测工具的结果目录和文件。


实验说明持续汇总整体结论，迭代说明记录本轮假设、设计、改动和结果解释，可分别参考[实验模板](../templates/experiment-template_zh.md)和[迭代模板](../templates/iteration-template_zh.md)。

通过 `--output experiment`、`--output-dir results/<goal>/<motivation>` 和 `--iteration <name>` 将 perf/eval 命令归入同一轮迭代；重复执行会新增独立运行记录，不覆盖旧结果。

选择该输出方式后，`generated/` 保存采集到的命令、耗时、状态和可获取的源码快照，`artifacts/` 保存评测配置与结果。开发者或 Agent 维护说明，并引用这些记录。在包含改动的源码仓库中执行命令，以捕获对应源码状态；服务代码若单独构建，在说明中另行记录其来源。

## 每轮结束后

1. 查看本轮每次运行的退出状态、指标和模型输出。在 `iterations/<name>/notes/iteration.md` 中链接对应记录；失败或中断的运行注明原因和可用证据。
2. 更新同一份迭代说明：写清实际改动、与参考结果的差异、假设是否得到支持，以及调查、设计、部署、评测和分析的耗时。证据不足时写明还缺哪项测量。
3. 写下并执行本轮决策：保留、继续修改或回退本轮改动。记录实际留下的代码和部署状态，以及下一轮要验证的问题；回退只针对本轮改动。
4. 更新实验根目录的 `notes/experiment.md`：汇总本轮新增或修正的结论，链接迭代说明。具体结果留在各轮记录中，不覆盖旧运行产物。
5. 将经过验证、可重复使用的方法补充到 `guidance/`；只适用于某项任务的步骤更新到对应 `tasks/` 手册。注明适用条件并引用证据，尚未证实的猜想留在实验说明中。
6. 清理本轮不再使用的临时环境、后台任务和缓存。先确认资源归属与使用情况，保留结果、源码记录和下一轮要复用的资源，并在迭代说明中写清保留用途。

同一方案补跑时继续更新该轮说明并新增运行记录；开始不同方案时使用新的迭代名称。

## 解释结果

说明测量支持或否定了哪个假设，还有什么未得到解答。比较时考虑负载、精度、缓存状态或硬件差异，链接支撑结论的测量和模型输出。

最后可选地复核实现和原始输出，检查针对测试的取巧实现、模型行为变化，以及收益归因是否有据可依。

## 记录迭代耗时

记录调查、设计、实现、部署、评测和分析所花的时间。有实测值时引用记录，估算值注明来源。区分整条命令耗时与其中的测量窗口，包含关系的时间不重复相加。

根据这些耗时判断下一轮可以复用或简化哪些工作，并只选择能回答当前问题的评测。

# 实验记录

[English](experiments.md) | 简体中文

把同一目标下的方案和测量放在一起，方便后续尝试利用已有发现。实验对应整体目标，迭代对应一轮方案，运行对应一次性能测试或质量评测命令。

## 选择评测

每轮按问题选择评测，不必运行全部套件。具体命令见[实验命令参考](../../benchmarks/docs/recipes_zh.md)，使用其中的命令时将其输出选项改为本页的 `experiment` 设置。

| 要回答的问题 | 使用入口 | 重点查看 |
| --- | --- | --- |
| 延迟、解码速度或吞吐是否改善 | [性能评测](../../benchmarks/docs/perf/README_zh.md)，`foretoken perf` | 成功数、延迟分布、TPOT、吞吐及实际输出长度 |
| 回答质量是否变化 | [质量评测](../../benchmarks/docs/eval/README_zh.md)，`foretoken eval` | 得分、样本数、评分方式和逐题回答 |
| 时间花在计算、通信还是等待 | [性能剖析](../../benchmarks/docs/profile/README_zh.md)，`foretoken perf --profile` | 执行时间线；速度对比另用不带剖析的运行 |
| 并发、输入长度或流量变化有何影响 | [参数扫描与负载配置](../../benchmarks/docs/recipes_zh.md) | 各负载点的结果，而非仅看总平均值 |

解码优化任务见[Qwen 解码速度优化](../tasks/qwen-decode_zh.md)。命令选项可用 `foretoken perf --help` 和 `foretoken eval --help` 查询。

## 组织记录

先确定实验目录 `results/<优化目标>/<实验动机>/`，再为本轮方案选择迭代名称，例如 `queue-aware-routing`。目录结构如下，包含同一方案的两次性能测试和一次质量评测，以及另一个方案的记录位置。

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

运行 `foretoken perf` 或 `foretoken eval` 时，设置 `--output experiment`、`--output-dir results/<goal>/<motivation>` 和 `--iteration <name>`。将占位符替换为选定的目标、动机和迭代名称；同一方案的命令使用相同的输出目录和迭代名称，程序为每次运行新增目录。不指定 `--iteration` 时，每条命令创建一个新的编号迭代。

程序只在说明文件不存在时创建空白模板；开发者或 Agent 补充内容，后续运行不覆盖说明。在包含改动的源码仓库中执行命令，以捕获对应源码状态；服务代码若单独构建，在说明中另行记录其来源。

## 查看本次结果

先在 `iterations/<name>/runs/` 中找到本次运行。评测程序打印的具体结果目录位于其 `artifacts/` 下；一条扫描命令可能包含多个结果目录。

| 顺序 | 文件 | 查看内容 |
| --- | --- | --- |
| 1 | `generated/context.json` | 完成、失败或中断状态，退出码、命令和源码采集情况 |
| 2 | 结果目录中的 `config.json`、`environment.json` | 负载、生成参数、客户端与服务环境是否具有可比性 |
| 3 | 结果目录中的 `metrics.json` | 性能指标或质量评分；同时检查成功数、样本数和评分方式 |
| 4 | 性能结果的 `raw_output.json`，或质量结果的 `native/` | 原始响应、逐题回答及结束情况，验证汇总指标是否对应真实行为 |
| 5 | `generated/run.log`（启用 `quiet` 时）、评测日志或 `evaluator.log` | 失败原因、准备阶段和实际执行过程 |

质量评测需要逐题记录时添加 `--log_samples`（lm-evaluation-harness）。资源与服务指标的曲线入口见[性能结果](../../benchmarks/docs/perf/wandb_zh.md)；选择 W&B 输出前先完成登录。

要重画已有结果，将 `RESULT_DIR` 设为评测程序打印的具体结果目录，而不是整个实验目录：

```bash
foretoken plot "$RESULT_DIR" --columns 2
```

这条命令不重新推理。采集过性能剖析时，按[查看结果](../../benchmarks/docs/profile/README_zh.md#查看结果)使用 `foretoken profile view` 打开时间线。检查完结果后，再补充下面的手写说明。

## 每次运行后补充说明

命令结束后，打开 `iterations/<name>/notes/iteration.md`，由用户或 Agent 追加本次运行的说明，而不是修改本目录中的参考模板。至少写清：

- 本次运行的链接，以及测试了哪个改动或假设。
- 与参考结果相比发生了什么，模型输出是否符合预期，现有证据支持什么结论。
- 失败、中断或结论不明确时的原因和下一步；补充程序未记录的工作耗时。

每次补跑保留此前说明，以运行名称区分各次解释。原始指标和日志留在对应运行目录，说明中引用它们。程序不会自动填写这些分析。

## 每轮结束后

1. 查看本轮每次运行的退出状态、指标和模型输出。在 `iterations/<name>/notes/iteration.md` 中链接对应记录；失败或中断的运行注明原因和可用证据。
2. 更新同一份迭代说明：写清实际改动、与参考结果的差异、假设是否得到支持，以及调查、设计、部署、评测和分析的耗时。证据不足时写明还缺哪项测量。
3. 写下并执行本轮决策：保留、继续修改或回退本轮改动。记录实际留下的代码和部署状态，以及下一轮要验证的问题；回退只针对本轮改动。
4. 更新实验根目录的 `notes/experiment.md`：汇总本轮新增或修正的结论，链接迭代说明。不覆盖旧运行产物。
5. 有可复用的发现时，将方法及适用条件补充到 `guidance/`；任务专用步骤更新到对应 `tasks/` 手册，并引用验证证据。
6. 清理本轮不再使用的临时环境、后台任务和缓存。先确认资源归属与使用情况，保留结果、源码记录和下一轮要复用的资源，并在迭代说明中写清保留用途。

同一方案补跑时继续更新该轮说明并新增运行记录；开始不同方案时使用新的迭代名称。

## 解释结果

说明测量支持或否定了哪个假设，还有什么未得到解答。比较时考虑负载、精度、缓存状态或硬件差异，链接支撑结论的测量和模型输出。

最后可选地复核实现和原始输出，检查针对测试的取巧实现、模型行为变化，以及收益归因是否有据可依。

## 记录迭代耗时

在本轮迭代说明中分别记录调查、设计、实现、部署、评测和分析所花的时间。有实测值时引用记录，估算值注明来源。区分整条命令耗时与其中的测量窗口，包含关系的时间不重复相加。

根据这些耗时判断下一轮可以复用或简化哪些工作。

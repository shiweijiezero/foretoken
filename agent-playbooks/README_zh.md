# 推理系统优化任务手册

[English](README.md) | 简体中文

我们希望 Agent 能在 Foretoken 上自主、独立地持续工作，优化推理系统的性能、资源效率和服务质量。

我们提供了任务手册，方便开发者和 Agent 查找参考资料、开展优化实验并记录迭代结果。开发者可以直接使用，也可以据此指导 Agent 工作。实验中获得的方法和经验会逐步补充到手册中，供后续任务使用。

## 设计目标

- 快速部署：将代码改动快速更新到目标集群中的推理服务。
- 测试与反馈：验证性能、模型质量和资源开销，了解改动效果。
- 观测与诊断：结合日志、指标和性能剖析，定位运行问题与性能瓶颈。
- 实验记录：关联每轮改动、运行配置、结果和决策，方便比较与复现。
- 参考与经验积累：查找、补充参考资料，将实验经验整理为可复用的指导。
- 行为与归因检查：检查模型实际行为，识别指标投机，判断收益是否确实来自预期改动。

## 目录结构

| 目录 | 用途 |
| --- | --- |
| [templates/](templates/README_zh.md) | 实验、迭代记录与知识条目的参考模板 |
| [guidance/](guidance/README_zh.md) | 推理系统优化所需的参考资料与知识 |
| [tasks/](tasks/README_zh.md) | 常用优化任务的目标、环境与优化范围 |

## 使用步骤

1. 明确要改善的行为和判断标准，例如降低给定负载下的首 token 延迟，同时保持回答质量。
2. 确定优化范围：聚焦路由、推理引擎等具体部分，或面向整个系统，不限定组件。
3. 阅读代码、查阅相关研究和实践，结合已有测量了解系统现状；必要时补充测量。
4. 分析问题成因与改进空间，提出自己的假设并设计方案，说明预期效果，选择能验证假设的负载与指标。缺少可比结果时，先测量改动前的表现。
5. 修改代码或配置，按[源码部署流程](../docs/custom-deployment_zh.md)更新推理服务。
6. 执行选定的[评测](../benchmarks/README_zh.md)，比较改动前后的结果，结合模型输出、日志和性能剖析判断差异来源。
7. 每次评测结束后，由用户或 Agent 在 `results/<目标>/<动机>/iterations/<名称>/notes/iteration.md` 中追加运行链接、改动说明、结果解释和耗时；本轮结束后更新实验根目录的 `notes/experiment.md`。写法参考[记录模板](templates/README_zh.md)，目录见[组织记录](#组织记录)。
8. 根据结果保留、调整或回退改动，再选择下一轮要解决的问题。将可复用的发现补充到指导和参考资料中。
9. 可选：复核实现和原始结果，检查针对测试的取巧实现、模型行为变化或比较条件差异，确认收益归因是否成立。

## 选择评测

按本轮优化目标选择评测。具体命令见[实验命令参考](../benchmarks/docs/recipes_zh.md)，使用其中的命令时将其输出选项改为本页的 `experiment` 设置。

| 评测目的 | 工具与命令 | 查看内容 |
| --- | --- | --- |
| 比较延迟、解码速度和吞吐 | [性能评测](../benchmarks/docs/perf/README_zh.md)，`foretoken perf` | 成功数、延迟分布、TPOT、吞吐及实际输出长度 |
| 评估回答质量 | [质量评测](../benchmarks/docs/eval/README_zh.md)，`foretoken eval` | 得分、样本数、评分方式和逐题回答 |
| 定位计算、通信和等待开销 | [性能剖析](../benchmarks/docs/profile/README_zh.md)，`foretoken perf --profile` | 执行时间线；速度对比另用不带剖析的运行 |
| 比较不同并发、输入长度和流量下的表现 | [参数扫描与负载配置](../benchmarks/docs/recipes_zh.md) | 各负载点的指标及变化趋势 |

命令选项可用 `foretoken perf --help` 和 `foretoken eval --help` 查询。

## 组织记录

实验对应整体目标，迭代对应一轮方案，运行对应一次性能测试或质量评测命令。说明写在 `results/` 中，由用户或 Agent 补充；命令自动保存运行证据。

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

`--output experiment` 按上述结构保存结果。`context.json` 记录命令、状态、耗时和源码信息；`changes/` 在可采集源码时保存改动文件，保持仓库相对路径。`run.log` 在启用 `quiet` 时保存。`artifacts/` 内保留评测工具的结果目录和文件。

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

质量评测需要逐题记录时添加 `--log_samples`（lm-evaluation-harness）。资源与服务指标的曲线入口见[性能结果](../benchmarks/docs/perf/wandb_zh.md)；选择 W&B 输出前先完成登录。

要重画已有结果，将 `RESULT_DIR` 设为评测程序打印的具体结果目录：

```bash
foretoken plot "$RESULT_DIR" --columns 2
```

这条命令不重新推理。采集过性能剖析时，按[查看结果](../benchmarks/docs/profile/README_zh.md#查看结果)使用 `foretoken profile view` 打开时间线。检查完结果后，再补充下面的手写说明。

## 每次运行后补充说明

命令结束后，打开 `iterations/<name>/notes/iteration.md`，由用户或 Agent 追加本次运行的说明：

- 本次运行的链接，以及测试了哪个改动或假设。
- 与参考结果相比发生了什么，模型输出是否符合预期，现有证据支持什么结论。
- 失败、中断或结论不明确时的原因和下一步；补充程序未记录的工作耗时。

每次补跑保留此前说明，以运行名称区分各次解释。原始指标和日志留在对应运行目录，说明中引用它们。

## 每轮结束后

1. 在 `iterations/<name>/notes/iteration.md` 中汇总各次运行的结论：假设是否得到支持，负载、精度、缓存或硬件差异是否影响判断，还有什么问题未解决。可选地复核实现与原始输出，检查测试取巧和模型行为变化。
2. 决定保留、继续修改或回退本轮改动，记录执行后的代码与部署状态及下一轮问题。回退只针对本轮改动。
3. 更新实验根目录的 `notes/experiment.md`，写入新增或修正的结论并链接本轮说明，保留各次运行的原始产物。
4. 有可复用发现时，将知识和适用条件补充到 `guidance/`，任务专用方法补充到 `tasks/`，引用验证证据。
5. 核对归属和使用情况后，清理本轮不再使用的临时环境、后台任务和缓存；保留实验记录及下一轮所需资源，并在迭代说明中注明保留用途。

同一方案补跑时继续更新原迭代说明；开始不同方案时使用新的迭代名称。

## 记录迭代耗时

在本轮迭代说明中分别记录调查、设计、实现、部署、评测和分析所花的时间。有实测值时引用记录，估算值注明来源。区分整条命令耗时与其中的测量窗口，包含关系的时间不重复相加。

根据这些耗时判断下一轮可以复用或简化哪些工作。

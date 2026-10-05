<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 记录迭代实验

[English](experiments.md) | 简体中文

在 `--output` 中加入 `experiment`，即可把多次测量、代码改动和实验说明放在一起。结果路径按“优化目标／本次实验动机”组织，迭代名称则描述正在尝试的方案。

## 运行一轮实验

在包含本次改动的源码目录执行。以下命令使用[快速开始](../../README_zh.md#快速开始)准备的部署：

```bash
foretoken perf examples/quickstart --num-prompts 20 \
  --output experiment --output-dir results/reduce-ttft/queue-aware-routing \
  --iteration baseline

foretoken eval examples/quickstart --tasks gsm8k --limit 20 \
  --output experiment --output-dir results/reduce-ttft/queue-aware-routing \
  --iteration baseline
```

两次命令归入同一个 `baseline` 迭代，各自保存运行结果：

```text
results/reduce-ttft/queue-aware-routing/
├── notes/
│   └── experiment.md
└── iterations/
    └── baseline/
        ├── notes/
        │   └── iteration.md
        └── runs/
            ├── perf-000001/
            │   ├── generated/
            │   │   ├── context.json
            │   │   └── changes/
            │   └── artifacts/
            └── eval-000001/
```

`notes/` 留给用户或智能体填写说明，程序只创建一次空白模板，后续不更新其中的内容。`generated/` 保存自动采集的运行事实，`artifacts/` 保存评测产物，包括参数扫描点、模型比较和 `perf --profile` 的采集记录。重新绘图或恢复质量评测时，使用评测程序打印的具体结果目录。

重复执行会创建新的运行目录，不覆盖上次结果。尝试另一种方案时，换一个 `--iteration` 名称；不传名称则每条命令自动创建新的编号迭代。并发命令会分别取得不同的运行目录。一条命令中的参数扫描或多模型比较始终属于同一次运行。

`experiment` 已包含本地保存，无需额外传 `local`，也可与 `wandb`、`plot` 组合。添加 `quiet` 后，整条命令的准备和执行日志保存在 `generated/run.log`，终端不再打印进度；各次评测自己的日志仍随产物保存在 `artifacts/` 下。实验说明和源码快照仅保存在本地，不随评测附件上传。

## 查看和补充记录

`generated/context.json` 保存命令、起止时间、总耗时、退出状态和源码版本。命令中的凭据参数以及可能携带凭据的评测器模型参数会被隐藏。失败或中断后，已有记录仍会保留。源码采集耗时单独记录，评测指标则继续使用各自的测量窗口。

在实验根目录的 `notes/experiment.md` 中写明目标和比较方法，在每轮的 `notes/iteration.md` 中补充假设、改动、结果解释和下一步。说明中链接自动生成的记录和评测结果，不修改原始产物，也不重复抄录其中的指标。

根据当前问题选择[实验命令示例](recipes_zh.md)，不必每轮都运行完整套件。记录查找参考、修改、部署和分析所花的时间，估算值注明来源。解释结果时，说明工作负载、精度、缓存或硬件的变化是否也可能带来观察到的差异。

## 还原代码状态

每次命令记录当前仓库的 `HEAD`，并将修改过的文件、未被忽略的新增文件复制到 `generated/changes/`，保持仓库相对路径。`generated/context.json` 的源码条目同时记录删除操作、符号链接目标、文件权限，以及已初始化子模块的版本和改动。中途提交或推送不影响记录：每次运行以自己的提交版本为起点，不维护整个实验共用的基线版本。

还原时，在独立目录检出记录中的提交，按条目执行删除和符号链接操作，再覆盖快照文件并恢复其权限。还原的是工作区内容，而非当时的暂存状态。被忽略的文件和仓库之外的引擎源码不会复制；下载的权重和缓存应放在源码目录之外，或由 Git 忽略规则排除。

这份快照对应命令执行位置的源码，不等同于服务正在运行的代码。评测结果中的 `environment.json` 另行记录客户端环境，并在 Kubernetes 模式下记录观测到的服务配置和运行时镜像。使用单独构建的运行时或其他引擎源码时，在实验说明中补充其来源。在 Git 仓库之外运行，记录会注明无法采集源码。

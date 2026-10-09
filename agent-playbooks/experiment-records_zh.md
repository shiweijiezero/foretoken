<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 实验记录手册

[English](experiment-records.md) | 简体中文

## 选择实验命令

根据优化目标，从[实验命令参考（recipes）](../benchmarks/docs/recipes_zh.md)中选择合适的命令，并按待测模型和实验假设调整负载与参数。

例如：

| 想测什么 | 选择 recipes 中的哪一组 |
| --- | --- |
| 不同输入输出长度、并发下的性能 | [输入输出长度与并发](../benchmarks/docs/recipes_zh.md#输入输出长度与并发) |
| 输入变长对性能的影响 | [长上下文性能](../benchmarks/docs/recipes_zh.md#长上下文性能) |
| 多轮对话负载下的表现 | [多轮对话：每秒启动多少段](../benchmarks/docs/recipes_zh.md#多轮对话每秒启动多少段) |
| 满足延迟目标时能承载多少流量 | [SLO 达标率与 goodput](../benchmarks/docs/recipes_zh.md#slo-达标率与-goodput) |
| 按真实请求时间分布测量 | [真实时间戳回放](../benchmarks/docs/recipes_zh.md#真实时间戳回放) |

## 保存实验记录

在 recipe 命令中调整以下参数：

| 参数 | 填写方式 |
| --- | --- |
| `--output` | 用逗号组合：`experiment` 保存实验记录（含本地结果），`plot` 导出图表，`wandb` 上传结果，`quiet` 将终端进度和汇总改为写入日志，错误仍显示，`raw` 保存文本 HTTP 性能评测的实际请求与响应。 |
| `--output-dir` | Agent 自定实验根目录，建议 `results/<优化目标>/<实验名称>`；同一实验的不同方案共用目录。 |
| `--iteration` | Agent 自定方案名。同一方案补跑沿用名称，换方案时更名，每次运行生成独立记录；省略时自动创建编号迭代。 |

已登录 W&B 时，推荐 `experiment,wandb,plot`，方便用户与 Agent 查看和比较结果；未登录时使用 `experiment,plot`，保存本地记录和图表。需要检查实际输入与回答时再加 `raw`。

例如，在已登录 W&B 的情况下，将 recipe 的输出参数改为：

```bash
--output experiment,wandb,plot \
--output-dir results/decode/parallelism \
--iteration tp2
```

## 找到实验记录

实验目录由 `--output-dir` 指定，方案目录由 `--iteration` 命名：

```text
results/decode/parallelism/
├── notes/experiment.md
└── iterations/tp2/
    ├── notes/iteration.md
    └── runs/perf-000001/
        ├── generated/
        │   └── context.json
        └── artifacts/
            └── <具体结果目录>/
                ├── config.json
                ├── environment.json
                ├── metrics.json
                └── …
```

记录保存在执行评测命令的机器上，相对路径以该命令的工作目录为起点。在远端运行时，到远端按命令打印的路径查看。参数扫描的一次运行可以包含多个结果目录；使用 W&B 时，也可通过输出的链接查看曲线和比较结果。

## 分析结果

下表列出常用实验文件。以上例的 `results/decode/parallelism/iterations/tp2/runs/perf-000001/` 为起点，`generated/` 下保存命令与源码记录，`config.json` 等评测文件位于 `artifacts/<具体结果目录>/`。根据实验类型和输出选项，还会产生预热记录、GPU 分配、Prometheus 观测、评测框架报告等文件，按分析需要查看。

| 顺序 | 读取内容 | 分析重点 |
| --- | --- | --- |
| 1 | `generated/context.json` | 命令是否完成、退出状态及本次源码记录 |
| 2 | `config.json`、`environment.json` | 负载、生成参数、客户端版本与服务配置是否可比；Foretoken 配置模式还记录服务镜像、Pod、资源声明、运行身份变化及可取得的构建源码来源 |
| 3 | `metrics.json`、结果图表 | 成功数、实际输出长度与目标指标如何变化，是否支持本轮假设 |
| 4 | `raw_output.json`；启用 `raw` 时的 `responses.jsonl` | 定位异常请求，以 `request_id` 对照指标、停止原因与实际回答；质量评测查看 `native/` 中的原生报告和已保存的逐题记录 |
| 5 | 启用 `quiet` 时的 `generated/run.log` 和结果目录中的 `run.log`；质量评测的 `evaluator.log` | 定位客户端准备与执行中的错误；结合已有资源观测和 profile 解释性能差异，确定下一步补测。模型加载或引擎错误查看对应服务端日志。 |

## 完善实验说明

每次运行后，在本轮 `notes/iteration.md` 中追加：

- 运行目录或 W&B 链接，以及测试的改动或假设。
- 与参考结果的差异、原因分析和证据支持的结论。
- 保留、调整或回退的决定，以及下一步实验。

一轮结束后，将主要结论和迭代链接汇总到 `notes/experiment.md`。指标与日志保留在原运行目录，说明中引用已有产物；失败和中断的运行也记录原因。写法参考[实验与迭代模板](templates/README_zh.md)。

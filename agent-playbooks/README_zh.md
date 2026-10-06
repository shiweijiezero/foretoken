# 推理系统优化任务手册

[English](README.md) | 简体中文

通过任务指导和实验说明，把 Foretoken 的代码改动、部署、测试负载与测量结果联系起来。

使用[通用模板](common/README_zh.md)记录实验和每轮尝试，通过[源码部署流程](../docs/custom-deployment_zh.md)应用改动，再用现有[评测工具](../benchmarks/README_zh.md)测量结果。

## 内容组织

| 目录 | 用途 |
| --- | --- |
| [common/](common/README_zh.md) | 各任务共用的实验与迭代说明模板 |
| [guidance/](guidance/README_zh.md) | 可复用的架构、代码与测量指导 |
| [tasks/](tasks/README_zh.md) | 各类优化任务的操作手册 |

## 编写任务手册

明确目标、改动范围和相关代码，再给出与参考结果比较所需的部署与测量步骤。说明如何解释观察结果、判断是否保留改动；共用指导和已有命令直接引用，不重复编写。

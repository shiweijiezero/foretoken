<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 优化任务模板

[English](task-template.md) | 简体中文

复制并填写下面的任务描述。单服务配置写明固定条件或探索起点；可用资源是整个任务的资源池，可分配给并行实验。

```text
模型：<模型名称，可列多个>
精度：<如固定 BF16，或比较 BF16 与 FP8>
部署形态：<如聚合部署、预填充/解码分离>
单服务配置：<GPU 数与 TP/PP/DP/EP 配置，注明固定项或可调整范围>
可用资源：<如 2*8 卡 C500>
评测负载：<数据集或真实请求、输入输出长度、并发或请求速率，可为多组>
优化目标：<各组负载或整体的吞吐、延迟、质量、资源效率目标>
可改动范围：<如部署参数、推理引擎、请求路由，或不限定组件>
工作与结束条件：<如持续优化 12 小时、达到目标结束，或在 12 小时内达到目标>
交付结果：<保留方案、代码改动、结果对照与实验记录>
```

TP、PP、DP、EP 分别表示张量、流水线、数据和专家并行。评测负载可以引用已有配置或 [recipes](../../benchmarks/docs/recipes_zh.md)；多组负载的目标应说明按各组还是整体结果判断。

填写例子见[优化任务](../tasks/README_zh.md#举例)。执行沿用[通用流程](../README_zh.md#使用步骤)，运行结果按[实验记录手册](../experiment-records_zh.md)保存。

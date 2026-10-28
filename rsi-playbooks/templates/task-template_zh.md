<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 优化任务模板

[English](task-template.md) | 简体中文

按需填写任务描述，只固定真正关心的条件；其余可写“不限”“由 Agent 选择”或省略。最终部署预算与可选起始配置分开填写，可用资源描述整个任务实际可调配的资源池。

```text
执行入口：<工作目录或仓库、部署配置或服务地址；上下文已明确时可省略>
起始配置：<可选，维护中的部署目录或已可用的配置；不提供时由 Agent 自主探索初始方案>
历史实验：<已有实验目录或报告、主要结论与待解决问题；没有则省略>
模型：<指定模型或范围，也可由 Agent 选择>
精度：<如固定 BF16、比较 BF16 与 FP8，或不限>
部署形态：<如聚合部署、预填充/解码分离，或不限>
最终部署预算：<保留方案的节点安排、GPU／CPU／内存总上限，说明涵盖的工作负载；也可由 Agent 选择>
可用资源：<节点数与每节点可用的 GPU、CPU 核数、内存，如 2*8 卡 C500，每节点 64 核 CPU、512 GiB 内存>
Explorer 数量：<该角色并行子 Agent 数，不含主 Agent；非负整数，留空由主 Agent 安排>
Worker 数量：<该角色并行子 Agent 数，不含主 Agent；非负整数，留空由主 Agent 安排>
Experimenter 数量：<该角色并行子 Agent 数，不含主 Agent；非负整数，留空由主 Agent 安排>
Evaluator 数量：1
Evaluator 巡查间隔：<两轮团队巡查之间的时间，例如 1h>
Codex 模型：Explorer = gpt-6-astra/high；Worker、Experimenter、Evaluator = gpt-6.1-sol/high
Claude 模型：Explorer = Fable/Opus/high；Worker、Experimenter、Evaluator = sonnet/high
最大并行实验数：<同时运行的独立实验上限，如 4；或由 Agent 根据资源预算确定并记录>
评测负载：<日常迭代的低成本负载，以及何时扩大或进行最终验证；指定数据集、长度、并发等，或由 Agent 选择>
优化目标：<主指标与目标值、需保持的条件、按各组或整体判断；也可开放探索>
可改动范围：<如部署参数、推理引擎、请求路由，或不限定组件>
工作与结束条件：<计时起点与总工作时长或达标条件；单轮实验预算可指定或由 Agent 根据假设选择>
交付结果：<完整迭代过程：每轮动机、方案与改动、测量结果、分析、耗时和下一步决定；最终收益、保留方案、代码改动与结果链接>
```

先核对起始配置与任务及当前环境是否匹配，再在允许范围内演进。没有提供时，自主选择初始部署并记录，再进行对照；历史保留结果在条件仍适用时继续复用。

Evaluator 数量指每轮新建的巡查 Agent 数。省略或留空时默认 1，设为 0 则关闭巡查。启用时在这里填写间隔；未填写的间隔由主 Agent 在执行前补齐，并按 [RSI skill](../../.agents/skills/rsi/SKILL_zh.md#定期检查团队状态)安排提醒和每轮新建。

最大并行实验数约束同时开展的独立实验；请求并发在评测负载中设置。区分最终部署预算与单次实验需求，小型诊断可以使用更少设备，将余下资源安排给其他有效工作，同时保持对照条件。

评测负载可以引用已有配置或 [recipes](../../benchmarks/docs/recipes_zh.md)；多组负载的目标应说明按各组还是整体结果判断。

填写可参考适用于 [NVIDIA](../tasks/qwen3.5-35b-a3b-nvidia_zh.md) 和[沐曦](../tasks/qwen3.5-35b-a3b-muxi_zh.md)的 Qwen3.5-35B-A3B 优化任务。Agent 按 [RSI skill](../../.agents/skills/rsi/SKILL_zh.md)执行，运行结果按[实验记录手册](../experiment-records_zh.md)保存。

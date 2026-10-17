<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按草稿路径置信度分配候选树验证节点

[English](tree-confidence-budget.md) | 简体中文

## 现象与动机

候选树提供替代分支，可以挽救早期错误猜测。但目标要计算全部保留节点，响应只提交一条路径，节点增加不直接等于输出增加。固定验证预算需要在深度、替代选择和执行成本之间分配。

## 原理与分析

EAGLE-2 用根到节点的 draft 概率乘积打分。后代继承所有祖先的不确定性，弱首候选下面的高置信 token 并不独立有用。全局排序并在同分时优先浅节点，可保持祖先闭包。分支互斥，节点存活得分之和是构树启发式，不是所有随机树验证器的期望输出。

## 设计

使用一个目标和一个匹配训练 EAGLE 模块，而不是每分支一个模型。扩展有希望的前沿，按预算保留节点，再展平交给目标。位置和 mask 只允许每节点读取提交前缀与自身祖先，兄弟不能互相 attention。贪心验证沿目标匹配子节点前进直到校正；随机验证使用引擎树概率耦合、proposal 概率和兄弟记账，而不是独立 p/q 检查后选择最长分支。

## 实现

概念算法为：

1. 从提交根节点扩展 draft 前沿，保存 token、父节点和条件概率。
2. 计算 path_score[node] = path_score[parent] × probability[node]。
3. 全局排序，同分优先浅节点，按预算保留祖先闭合集合。
4. 构造仅祖先可见的 attention mask 和位置，执行目标前向。
5. 验证一条合法路径，提交它及校正/bonus，丢弃其他节点。

固定 SGLang 的 EAGLE worker 负责执行。`--speculative-num-steps`、`--speculative-eagle-topk`、`--speculative-num-draft-tokens` 控制深度、分支、容量；自动选择时全不设，手动时全明确设置。topk=1 的容量为 steps+1。文档 overlap scheduler 只支持 topk=1，宽树走不同调度路径。匹配 EAGLE3 权重可更新候选来源，而不改变这套树外层流程。

## 影响与效果

路径置信分配减少弱前缀下的深层投入，并保留早期替代选择，同时增加 draft 展开轮次、树 mask、候选 LM-head 行和路径压紧。未选择节点不会成为输出。确定续写偏向深度，早期歧义偏向宽度；宽树还可能失去主机设备重叠，因此节点数相同不代表单轮时间相同。提交后只保留选中历史；不能由 KV 节点保留恢复的模型状态，见[循环回放](../attention-state/state-write-replay_zh.md)。

## 参考

- [EAGLE-2 原论文 v2](https://arxiv.org/html/2406.16858v2)：动态扩展、路径得分、重排序和树 mask（2024）。
- [固定提交的 SGLang 指南](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/docs/docs/advanced_features/speculative_decoding.mdx)：当前参数和 overlap 条件。[EAGLE worker](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/speculative/eagle_worker_v2.py) 是当前 worker 执行入口。
- [EAGLE-3 原论文](https://arxiv.org/html/2503.01840)界定特征条件 draft 的独立训练来源；对应职责详见[训练对齐](draft-rollout-training_zh.md)。

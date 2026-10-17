<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 选择高频形状专门化，权衡编译成本与执行收益

[English](compile-cost-specialization.md) | 简体中文

## 现象与动机

服务中的 token 数不断变化，按固定形状编译可能反复生成代码；全部使用动态形状，又可能失去常见尺寸的专用内核。短生命周期 worker 尤其容易把时间花在准备而非推理上。

## 原理与分析

符号维度让一个编译产物接受多个尺寸，但分支条件仍会产生 guard。例如代码对单 token 和多 token 使用不同算法，两条路径仍需不同产物。Triton 的 `constexpr` 或自定义算子把符号维度转成整数，也会引入专门化。

编译与 CUDA Graph 是两层：一个符号 Inductor 图可以处理多个尺寸，图重放通常仍为具体尺寸分别捕获。减少重编译不一定减少捕获或 padding 成本。

若专用版本新增准备成本为 `C`，每次相对通用版本节省 `Δt > 0`，忽略其他成本时，`ceil(C/Δt)` 次使用可摊平准备成本。形状频率与 worker 生命周期决定专门化是否划算。

## 设计

保持 head size、矩阵宽度等稳定维度为静态，频繁变化的 token 数使用符号维度。只为高频且有明显执行收益的尺寸保留静态版本；图捕获尺寸另按 padding 与内存成本选择。

vLLM v0.31.0 的编译集成以 token 维度符号化，并通过 `compile_sizes` 增加静态版本。模型编译由该入口管理。

## 实现

PyTorch v2.14 默认先按静态尺寸编译，尺寸变化触发重编译后，将变化维度转为动态。已知变化维度可以在编译函数外用 `mark_dynamic` 标注；有合法静态分支时，可用 `maybe_mark_dynamic` 允许分支专门化。

执行选择可概括为：

```text
准备：编译通用符号路径和选定的高频静态路径
每次执行：
    若形状匹配静态路径且 guard 成立，使用静态路径
    否则使用满足 guard 的符号路径
    图调度器再决定是否补齐并重放已捕获的图
```

guard 失败时追踪具体分支或元数据转换；算法确实不同时保留分支，等价计算上的意外具体化则可消除。

## 影响与效果

PyTorch 动态形状文档的简单示例依次输入长度 10、20、30、40：静态模式生成四个图，动态模式生成一个图。减少的是这些尺寸变化引起的重编译。

定向专门化保留常见形状的代码优化，同时控制产物数量与启动成本。低频形状或存活时间很短的副本可能无法摊平额外准备；过多捕获尺寸还会占用可用于 KV 的内存。

## 参考

- [PyTorch v2.14 动态形状](https://github.com/pytorch/pytorch/blob/v2.14.0/docs/source/user_guide/torch_compiler/torch.compiler_dynamic_shapes.md)：标注、guard 和图数量示例。
- [vLLM v0.31.0 编译集成](https://docs.vllm.ai/en/v0.31.0/design/torch_compile/)：符号 token 维度与静态编译尺寸。
- [PyTorch v2.14 CUDA Graph Trees](https://github.com/pytorch/pytorch/blob/v2.14.0/docs/source/user_guide/torch_compiler/torch.compiler_cudagraph_trees.md)：形状相关捕获；相关机制见[图分桶](launch-graph-buckets_zh.md)。

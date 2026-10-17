<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 权衡自定义内核控制与编译器可见融合

[English](custom-op-fusion.md) | 简体中文

## 现象与动机

原生内核单独执行更快，接入编译模型后却可能因阻断相邻融合、增加复制或打断图捕获而变慢。内核集成需要正确的编译接口，而不只有正确的算术。

## 原理与分析

自定义算子通过 schema、后端实现和只计算元数据的 FakeTensor 函数接入编译器。形状、stride、dtype、device、mutation 和 alias 描述下游变换可以采用的假设。编译器能追踪调用，但原生实现仍是不透明的。

以 `归一化 → 类型转换 → 投影` 为例，不透明替代可能挡住原先与投影融合的转换；输出 stride 声明错误会影响消费者选择，未声明的原地修改则会破坏复用或重排。边界位置同时影响优化空间和正确性。

## 设计

可见张量表达式允许编译器变换整个序列；不透明算子封装手调内核；模式匹配 pass 则在支持的形状和布局下，将已识别的序列替换成原生算子。

vLLM 的 Q/K 归一化与 RoPE 采用第三种设计。缓存生命周期仍归引擎，算子明确声明写入和别名关系。

## 实现

核心集成顺序为：

```text
注册 schema，声明被修改的参数和别名
注册后端内核与仅返回元数据的 FakeTensor 函数
用符号元数据追踪模型
匹配支持的算子序列
替换为原生调用，保留输出元数据
将不适合捕获的操作分离到图外
```

QK pass 匹配 packed QKV 拆分、逐 head RMSNorm 与 RoPE，通过 `auto_functionalized` 调用原生融合操作，再拆分返回的更新张量。因此，原生的 mutation 在编译图中表现为函数式结果。

PyTorch 默认将自定义操作视为可捕获。内部包含主机同步等不兼容行为时，用 `torch._C.Tag.cudagraph_unsafe` 标记，CUDA Graph partitioning 可将其留在捕获区域外。`opcheck` 检查注册集成，输出对照用于判断算法数值。

## 影响与效果

正确的元数据和 mutation 声明，使编译后的消费者能够使用原生内核而不引入多余 graph break。模式替换可以在选中融合序列之前，保留相邻优化机会。

不透明边界也会减少编译器可见性。源码算子数减少，不一定减少设备工作；新增中间张量、暂存或图分区可能抵消内核收益。真正有效的融合应减少完整生产者—消费者链上的开销。

## 参考

- [PyTorch C++／CUDA 自定义算子](https://docs.pytorch.org/tutorials/advanced/cpp_custom_ops.html)：schema、FakeTensor、mutation 和集成检查。
- [vLLM v0.31.0 编译设计](https://docs.vllm.ai/en/v0.31.0/design/torch_compile/)：自定义 attention 边界与分段编译。
- [QK 融合实现](https://github.com/vllm-project/vllm/blob/db9527a46873454610df6dbedf79a36d6bf1a7f6/vllm/compilation/passes/fusion/qk_norm_rope_fusion.py)。
- [PyTorch v2.14 图分区](https://github.com/pytorch/pytorch/blob/v2.14.0/docs/source/user_guide/torch_compiler/torch.compiler_cudagraph_trees.md)：不安全自定义算子与捕获行为。

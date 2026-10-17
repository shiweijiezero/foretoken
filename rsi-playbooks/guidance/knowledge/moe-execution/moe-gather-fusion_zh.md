<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 将输入收集融合到分组 GEMM，减少 MoE 中间张量写回

[English](moe-gather-fusion.md) | 简体中文

## 现象与动机

稀疏 MoE 的算术量较少，执行却可能主要花在收集激活、写出中间张量和合并输出上；专家中间维度较窄、每专家行数很少时尤其明显。SonicMoE 将输入收集和激活融合到 grouped GEMM，同时在有利于 I/O 重叠时保留独立输出归约。

## 原理与分析

输入为 `X[T,H]`，每 token 选择 `K` 个专家，专家 `e` 收到 `m_e` 行；全部路由有效时 `sum(m_e)=T*K`：

```text
H_e = gather(X, token_indices_e) @ W1_e
A_e = SwiGLU(H_e)
Y_e = A_e @ W2_e
y_t = sum_{e selected by t} routing_weight[t,e] * Y_e[row_e(t)]
```

独立 gather 会将路由后的激活写入 HBM，再被 GEMM 读取。按索引直接加载到共享内存，可消除这次物化；SwiGLU 放在 FC1 收尾阶段，则不必将激活前张量作为独立阶段暴露。

tile 浪费仍存在：专家行数 `[1,17,129]`、行 tile 为 128 时，147 个有效行占用 512 行 tile 容量。这是形状示例，不是实测加速比；掩码、算术和权重复用的成本不同。

## 设计

在不改变原始专家选择与权重的前提下构建专家行索引。Grouped FC1 直接消费索引并输出激活后中间结果，FC2 连续写出 expert-major 结果，再由 gather-and-sum 恢复 token 顺序并应用路由权重。

Hopper 上分散的全局写入可能阻碍收尾 I/O 与下一个 MMA tile 重叠。连续异步写出加独立归约，因此可能优于更激进的 scatter 融合。目标是降低关键路径上的昂贵流量，而不是单纯减少核函数数量。

## 实现

1. 统计有效路由，生成专家 offset，记录原 token 索引和各路由在专家中的局部行号。
2. FC1 将索引输入 tile 收集到共享内存，与对应专家 gate/up 权重相乘，在收尾阶段执行 SwiGLU。
3. FC2 处理激活后行，按专家连续写出结果。
4. 为每个 token 收集选中专家结果，乘原始路由权重并求和。

固定版本 `sonicmoe/functional/forward.py` 通过 CuTeDSL、QuACK、Triton 面向 Hopper/Blackwell。Checkpoint 的 gate/up 拼接布局须转换为要求的交错布局，保持两者角色不变；BF16 操作数和路由归一化也是执行约定。若 EP dispatch 已提供 expert-major 输入，就可能不再需要本地 gather。

## 影响与效果

输入收集融合节省路由激活的一次 HBM 写入和再读取，激活融合消除阶段边界及激活前张量物化。FC2 连续输出仍需被归约读取，但利于异步写出。SonicMoE 论文主要测训练，包含反向激活存储，不能据此量化 decode 的显存节省。可选 token-rounding router 改变专家选择，需要训练协同设计，与这里保持路由不变的执行优化不同。Tile 不满和最终聚合仍是成本。

## 参考

[SonicMoE 论文](https://arxiv.org/abs/2512.14080)的算法 1–4、第 3–5 节与附录 B/F，以及 [commit 7396f3e](https://github.com/Dao-AILab/sonic-moe/tree/7396f3e604827d8186c2e16e64b28ee33d3defd0) 的 `sonicmoe/functional/forward.py`、`benchmarks/moe-cute.py`。

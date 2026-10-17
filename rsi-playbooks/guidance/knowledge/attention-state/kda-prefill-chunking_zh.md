<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用分块循环计算并行执行 KDA 预填充

[English](kda-prefill-chunking.md) | 简体中文

## 现象与动机

循环注意力为每条序列更新一个矩阵。长 prompt 若逐 token 顺序执行依赖更新，就难以利用并行硬件；decode 每次处理一个 token 则天然适合这种路径。Kimi Delta Attention（KDA）引入通道级遗忘，并通过分块将大部分 prefill 工作转为矩阵乘法。

## 原理与分析

采用 key-first 状态 `S[K,V]`，列向量 `q,k` 长度为 `K`，`v,r,o` 长度为 `V`，通道 decay 为 `alpha[K]`，更新强度 `beta` 为标量。key 已归一化，`q` 包含输出缩放：

```text
A  = diag(alpha)
Sd = A*S
r  = v - transpose(Sd)*k
S' = Sd + beta*k*transpose(r)
o  = transpose(S')*q
```

等价地，`S'=(I-beta*k*k^T)*A*S+beta*k*v^T`。更新减去沿当前 key 已预测的 value，避免重复 key 像简单加法记忆那样不断累积。`alpha=[0.99,0.10]` 能保留第一行、强烈遗忘第二行，GDN 的标量 decay 无法在一个 head 内作这种区分。必须先衰减再计算 residual：对角矩阵通常不能与 `k*k^T` 交换位置。

## 设计

将 prefill 分块，利用累计对角衰减和低秩修正表达块内因果依赖，再通过三角变换生成辅助矩阵，使主要工作能够并行执行矩阵乘法。只有块边界状态顺序传递。Decode 则融合单 token 更新与读出。

KDA 需要经过相应训练的权重。Kimi Linear 每三层 KDA 后放一层全局 MLA，所以整个模型仍有随历史增长的 MLA 缓存。分块是在保持 recurrence 的前提下改变执行，不是把 hybrid 模型换成通用线性注意力。

## 实现

1. 按模型投影和 gate 规则生成 Q/K、value、通道 gate 与 beta，完成所需归一化。
2. 用 `cu_seqlens` 打包序列，计算块内累计 log-decay，建立因果三角修正系统。
3. 求解变换后的块内依赖，与传入状态组合，通过并行矩阵运算计算各 token 输出。
4. 生成块末状态交给下一块；最后一块状态以相同方向和缩放交给 fused-recurrent decode。

固定版本 FLA chunk API 支持块长 32 或 64，key dimension 最大为 256。显式 initial state 为 FP32，激活通常为 BF16。log-space gate、内核内转换的 raw gate、sigmoid 后的 beta 是不同输入。默认状态布局 `[N,HV,K,V]`，也支持 value-first。公开 recurrent wrapper 未暴露全部底层 state-index 与 accepted-token 参数，批处理 slot 仍由引擎管理。

## 影响与效果

分块将长串 token 依赖转为适合 Tensor Core 的块内工作与顺序边界交接，代价是辅助矩阵和临时存储。KDA 层固定大小的状态避免了逐 token K/V 增长，但 decode 仍须为每个活跃请求读写状态矩阵。不等长打包能保持不同序列的 recurrence 独立。CUDA、Ascend 后端各有实现，不能由后端存在推断调度相同。[重放缓冲区](state-write-replay_zh.md)另行减少状态写回，标量 decay 的重放公式不能直接实现 KDA 通道 gate。

## 参考

- [Kimi Linear，2025-10-30，v1](https://arxiv.org/html/2510.26692v1)：KDA recurrence、对角加低秩的分块执行和 hybrid 结构。
- [FLA chunk API，固定 `1fe52b5d05c53601885610fab3d39157647d836d`](https://github.com/fla-org/flash-linear-attention/blob/1fe52b5d05c53601885610fab3d39157647d836d/fla/ops/kda/chunk.py)：gate 输入、状态精度、打包和 chunk 限制。
- [同一 revision 的 fused-recurrent 实现](https://github.com/fla-org/flash-linear-attention/blob/1fe52b5d05c53601885610fab3d39157647d836d/fla/ops/kda/fused_recurrent.py)：更新顺序、状态布局、公开接口与底层接口的区别。

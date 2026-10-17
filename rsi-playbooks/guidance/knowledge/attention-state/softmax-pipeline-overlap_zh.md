<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用异步流水重叠注意力矩阵计算与 softmax

[English](softmax-pipeline-overlap.md) | 简体中文

## 现象与动机

长上下文预填充中，矩阵乘法已经很快，指数运算、行归约、概率转换和输出重缩放却仍可能占据主要时间。Tensor Core 越快，这种不均衡越突出；只扩大 GEMM tile 可能增加寄存器压力，却没有缩短 softmax 的依赖链。FlashAttention-4 用 Blackwell 异步流水重叠这些阶段。

## 原理与分析

稠密注意力可以逐块读取 K/V，同时维护每行最大值 `m`、分母 `l` 和未归一化输出 `O`。对已包含缩放和 mask 的分数 `S`，实数域中的在线更新为：

```text
m' = max(m, rowmax(S))
a  = exp(m - m')
P  = exp(S - m')
l' = a*l + rowsum(P)
O' = a*O + P*V
```

分母与累计输出必须使用同一个参考最大值。若后续出现更大的 logit 却不修正旧贡献，早先 token 就会被赋予过高权重。逐块计算消除了平方规模的分数存储；流水重叠进一步缩短执行路径，并不减少参与注意力的 token。

## 设计

FlashAttention-4 交错处理两个 query tile：Tensor Core 计算一个，softmax warpgroup 处理另一个；独立 correction warpgroup 修正累计输出。异步 MMA 将中间结果放入 Tensor Memory。较大的 tile 能摊薄行统计和调度成本，但也占用更多资源。

指数运算由特殊函数单元与多项式/FMA 分担。条件重缩放允许参考最大值暂时落后于较小的增长，减少向量修正，同时让概率、分母与输出保持一致的归一化基准。指数近似与激活舍入仍影响精度。

## 实现

1. 每个 query tile 初始化 `m=-infinity`、`l=0`、`O=0`，准备 Q 和第一块 K/V。
2. 异步发起 QK MMA，分数就绪后交给 softmax；同时准备或计算另一 query tile 的独立工作。
3. 生成概率与归一化统计，发起 PV MMA，并在复用存储前完成对应输出修正。
4. 遍历有效 K/V tile，最后完成修正并写出 `O/l`。

固定版本 CuTe 接口向 `FlashAttentionForwardSm100` 分派形状匹配的 tile、split 数及单/双 CTA 调度。SM100/SM110 forward 接受 FP16/BF16 和指定 FP8 格式，head dimension 有明确限制；FP8 输出为 BF16，descale 另有形状约定。分页、GQA、因果 mask 和短 query 会改变分派。Hopper 与非 CUDA 设备需要各自的内核。

## 影响与效果

该流水减少暴露在关键路径上的 softmax 与输出修正时间，保留完整稠密注意力范围。软件指数利用空闲 FMA 资源时才有价值；tile 过大或独立 query 太少，会因占用率下降、流水缺乏工作而削弱收益。split-KV 归约和引擎元数据准备不在这项节省之内。更快的长 prefill 也可能集中争用并发 decode 的资源，因此这里改善的是内核关键路径，并非所有服务负载的延迟都会同步下降。

## 参考

- [FlashAttention-4，2026 年 3 月，v1](https://arxiv.org/html/2603.05451v1)：forward 流水线、软件指数、条件重缩放及论文对比。
- [CuTe 接口，固定 revision `1fc0221183e799363001394a12dd9e78e15f5dda`](https://github.com/Dao-AILab/flash-attention/blob/1fc0221183e799363001394a12dd9e78e15f5dda/flash_attn/cute/interface.py)：架构、dtype、形状和调度分派。
- [同一 revision 的 SM100 forward 实现](https://github.com/Dao-AILab/flash-attention/blob/1fc0221183e799363001394a12dd9e78e15f5dda/flash_attn/cute/flash_fwd_sm100.py)：异步执行与输出修正阶段。

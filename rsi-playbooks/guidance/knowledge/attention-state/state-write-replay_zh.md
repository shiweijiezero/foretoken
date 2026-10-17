<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用重放缓冲区减少循环状态写回

[English](state-write-replay.md) | 简体中文

## 现象与动机

循环 decode 每步为每个请求、每层读写完整矩阵，推测验证还可能增加用于拒绝恢复的中间快照。高 batch 下，即使状态大小与历史无关，这些流量仍可能主导执行。ReplaySSM 用检查点加近期记录表示逻辑状态，避免在每个位置物化整个矩阵。

## 原理与分析

采用 value-first 状态 `S[V,K]`，列向量 `v_t,d_t` 长度为 `V`，`k_t,q_t` 长度为 `K`，GDN 的 `alpha_t,beta_t` 为标量：

```text
d_t = beta_t * (v_t - alpha_t*S_previous*k_t)
S_t = alpha_t*S_previous + d_t*transpose(k_t)
o_t = S_t*q_t
S_h = (product_i alpha_i)*S0
    + sum_j (product_{i>j} alpha_i)*d_j*transpose(k_j)
```

query 包含输出缩放。内核可从检查点与低秩记录计算 `S_h*q`，不把 `S_h` 写回 HBM；累计 `L` 条已提交记录后再 flush，摊薄整矩阵写入，但每步仍读检查点。GDN 每条修正依赖前一状态，verify chunk 因而需要联合三角变换，不能独立计算各 draft 更新。

## 设计

每个请求 slot 持有检查点、环形记录缓冲与明确的 committed cursor。验证三个 draft、只提交两个时，下一逻辑状态仅包含前两条，第三条可以覆盖；accepted length 的约定决定是否包含 bonus token。

将高精度 recurrence 检查点与低精度读出副本分开。保留原始 `v_t`，flush 从高精度检查点重算相互依赖的修正，再独立转换为两份状态。这样低精度读出生成的修正不会成为下一份永久检查点，但输出舍入仍然存在。

## 实现

1. 将当前输入或 draft 链输入写入请求的 replay 窗口，基于检查点与之前的有效记录计算修正和输出。
2. 仅为 accepted token 推进 committed cursor，在 folding 前排除被拒绝记录。
3. 到达 flush 边界后，从高精度检查点和已提交原始输入重建，更新两份检查点，回收已合并记录。
4. 导出前缀或传输时，物化逻辑状态，或一致地传输检查点及已提交记录；slot 复用时重置 cursor 和缓冲归属。

SGLang 的 PR #28451 已合并普通 decode，#28695 已合并推测集成。固定版本 `gdn_replayssm_spec_decode.py`、GDN backend 和 state pool 实现服务 cursor 与线性链验证。独立 compact-cache PR #27658 不属于这条已合并路径。KDA 通道 gate 需要不同的 replay 推导。

## 影响与效果

Replay 减少重复的完整状态写回和推测快照，代价是缓冲读取、重建与周期性 flush。增大 `L` 可省更多写入，也增加重放工作；前缀发布可能提前 flush 未满窗口。目标场景是大状态、带宽受限的循环层批处理，小批量仍可能受启动或计算限制。Hybrid 模型的整体收益受循环层耗时占比约束，MoE 与全局注意力并未改变。精度分离限制了误差向永久状态反馈，并不消除读出数值变化。

## 参考

- [ReplaySSM v1](https://arxiv.org/html/2610.13778v1) 与[提交记录](https://arxiv.org/abs/2610.13778)：v1 提交于 2026-10-09，介绍 recurrence、replay、speculation 与精度分离。
- [SGLang RFC #28511](https://github.com/sgl-project/sglang/issues/28511)：已关闭，描述集成范围和依模型而异的结果。
- [PR #28451](https://github.com/sgl-project/sglang/pull/28451)：合并 commit 为 `a10a24e9a7fdc0ddcc7dc3e90c0d4983707db028`；[PR #28695](https://github.com/sgl-project/sglang/pull/28695)：合并 commit 为 `c41c573ce9ed2053d38aa711582e19a67a5e9755`；[PR #27658](https://github.com/sgl-project/sglang/pull/27658)：仍开放，不作为已发布 compact-cache 能力依据。
- [实际 verify 内核，固定 `e9400c5a8f3a0d0312a37aecef694f5edfd69341`](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/kernels/ops/attention/fla/gdn_replayssm_spec_decode.py)：环形 replay、folding、commit 约定与精度路径。

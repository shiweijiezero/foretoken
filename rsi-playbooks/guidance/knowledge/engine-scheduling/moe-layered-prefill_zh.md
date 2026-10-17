<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按层组推进预填充，减少 MoE 专家权重流量

[English](moe-layered-prefill.md) | 简体中文

## 现象与动机

长输入与已有 decode 共用 GPU 时，需要限制每轮 prefill，避免生成停顿。Token 分块能缩短单轮执行，但每块都遍历全部层。MoE 的小块也可能合计激活大部分专家，让近似相同的 GPU 驻留权重被反复读取，每个专家却只处理少量 token。

## 原理与分析

Layered prefill 改为划分连续模型层组，而不只是切分输入。较大输入批量一次访问某组专家，让权重读取服务更多 token。它不减少任何必需的 token／层计算，已有 decode 也仍读取其选中专家。

收益取决于 token 块之间的专家重合度，以及专家 kernel 对权重的复用效率。稠密模型访问模式不同，作者在 Qwen3-8B 上观察到退化。放宽 token 间隔目标、允许更大 token 块后，优势缩小；纯 decode 没有额外的 prefill 权重读取可消除。

## 设计

新请求每轮推进一个层组，已有 decode 每轮仍遍历全部层。四层、八个输入 token 的例子中，按四 token 分块会两次访问层 1–4；按两层分组则先让八个 token 经过层 1–2，再从保存状态继续经过层 3–4。

新请求完成全部层组才能产生首 token。大层组减少转换，却延长 decode 等待；超长输入可先划成较大 token 块，再逐组推进，控制中间状态内存，同时保留较大的每专家批量。

## 实现

1. 分别记录 token 进度与下一层组；完成一组不等于完成整个模型的 prefill。
2. 对 prefill 批次运行指定连续层范围，同时让已有 decode 完成全层执行。
3. 保存层组输出的隐藏状态、所需残差、位置和 KV 映射；已完成层保留 KV，后续层等待执行。
4. 下一轮从保存状态继续；最后一组完成后才标记 prefill 结束并采样。
5. 将跨轮状态纳入取消、抢占、批次重组和 graph 生命周期。

公开原型基于 Nano-vLLM。引用版本的 vLLM 调度输出只有 token 数，没有层组进度，Qwen3 MoE forward 也不支持逐请求按组恢复。Pipeline parallelism 分层本身不等于该调度。

## 影响与效果

论文在两张 H100 80GB、NVLink、TP2 的混合服务中报告：相比 token 分块，Qwen3-30B-A3B 在 ShareGPT 上的专家权重流量从 28.5 TB 降至 25.1 TB（12%），在 arXiv 上从 35.6 TB 降至 21.7 TB（39%）。这是访存流量下降，不是服务速度提升百分比。保留较大输入批量还会增加中间状态内存与跨轮状态管理。

## 参考

- [From Tokens to Layers: Redefining Stall-Free Scheduling for MoE Serving with Layered Prefill，v2](https://arxiv.org/html/2510.08055v2)：调度机制与 MoE 服务结果。
- [Layered Prefill 实现与复现说明](https://github.com/scale-snu/layered-prefill/tree/053f80e5201a7c0ab56e468e3d578907a2ca9cc3)：包含依赖、FlashAttention 补丁、服务与测量命令。
- [当前调度输出](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/sched/output.py)
- [当前 Qwen3 MoE forward](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/models/qwen3_moe.py)

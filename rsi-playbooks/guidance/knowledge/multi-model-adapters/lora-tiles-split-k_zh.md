<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 加速 LoRA 推理：紧凑分块与 split-K 归约

[English](lora-tiles-split-k.md) | 简体中文

## 现象与动机

Punica、S-LoRA 已建立共享基础模型的多 LoRA 组批，但低秩更新仍可能使用低效 launch grid。不等宽打包 QKV 产生空 expand tile，小 decode batch 又使 shrink 并行度不足。SGLang PR #41951 于 2026-10-13 合并为 `c0584cf`，通过紧凑 expand 和可选 split-K shrink 改善这两类工作。

## 原理与分析

设 `X=[m,h]`、`W=[o,h]`，adapter 为 `A_i=[r_i,h]`、`B_i=[o,r_i]`，对应行计算 `Y=XWᵀ+s_i(XA_iᵀ)B_iᵀ`。基础 GEMM 一起处理各行，shrink 产生 rank 维中间值，expand 加回对应更新；base-only 行没有更新。

按 Q 最大宽度设置矩形 expand grid，会为窄 K/V 启动越界 tile。提前返回只省运算，不省启动；只启动有效 slice tile 才去掉这些工作。Shrink 则在 `m`、`r` 很小时沿大输入宽度归约；拆分输入维增加独立 program，同时需要 partial 存储和另一次归约。

## 设计

用 CPU 已知 slice offset 展平有效 expand tile，不从 GPU 拷回元数据。选定 shrink 形状按输入维分片，以 FP32 保存 partial，只归约活跃行和 rank 列后再转 dtype，避免读取未初始化的 graph padding。

合并 split-K 默认关闭，`SGLANG_ENABLE_LORA_CSGMV_SPLIT_K=1` 启用窄范围自动策略，针对已测 BF16 rank 32、宽度 4096 或 12288 和特定 tile 参数。Deterministic inference 保留原路径。这些条件不能当作所有低 rank 的通用规则。

## 实现

一个适配线性层的核心执行为：

```text
Y = base_GEMM(X, W)
按adapter分组活跃行，计算有效slice tile offset
遍历活跃adapter组:
    若可选split-K策略匹配形状、dtype和确定性模式:
        partials = 按输入维分片shrink(X, A, FP32累加)
        Z = 仅归约活跃partial并转换为输入dtype
    否则: Z = 直接shrink(X, A)
    只为该组有效输出slice启动expand tile
    Y[对应行] += scale * expand(Z, B)
返回Y
```

采用与基础 checkpoint、module、rank、scale 匹配的已训练 PEFT adapter，不用重训。TP 改变本地宽度；贡献者报告 H200 验证。引用 vLLM shrink 支持 FP16/BF16 输入及匹配 adapter，batch-invariant 路径使用 FP32 partial；量化 base 不代表量化 adapter。

## 影响与效果

紧凑 grid 减少不等宽投影的启动工作，不新增模型近似。Split-K 提高小批 shrink 并行度，却增加 scratch 流量与归约启动，因此较大 prefill 可能更适合直接 shrink；窄范围 opt-in 策略保留这一形状条件。

Split-K 改变求和顺序，不改变低秩公式。确定性路径继续使用原实现，普通推理按既定数值容差判断，不假定逐 bit 一致。Adapter kernel 只占层耗时一部分，base GEMM、routing、元数据和排队仍然存在。PR #41951 已合并，不是未合并提案，也不意味着所有模型服务都有端到端加速。

## 参考

- [SGLang PR #41951](https://github.com/sgl-project/sglang/pull/41951)：2026-10-13 合并，merge commit 为 `c0584cf9afefdb4a3004a57af8b1aec501d44a4a`，head 为 `a7deef700821501c37fd1de266d4f1f42848d23a`。合并后的 [shrink](https://github.com/sgl-project/sglang/blob/c0584cf9afefdb4a3004a57af8b1aec501d44a4a/python/sglang/kernels/ops/lora/dense/chunked_sgmv_shrink.py) 与 [expand](https://github.com/sgl-project/sglang/blob/c0584cf9afefdb4a3004a57af8b1aec501d44a4a/python/sglang/kernels/ops/lora/dense/chunked_sgmv_expand.py) 定义上述执行路径。
- [SGLang LoRA 文档](https://docs.sglang.io/advanced_features/lora.html)：Triton/CSGMV、组批和 graph 限制；main 版本为 `e9400c5`。[PR #42299](https://github.com/sgl-project/sglang/pull/42299) 已于 2026-10-03 合并，完成 kernel 目录重组。
- [vLLM shrink 源码](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/lora/ops/triton_ops/lora_shrink_op.py)：分组元数据与精度路径，commit `6849ca6`。
- [S-LoRA，arXiv:2311.03285v1](https://arxiv.org/html/2311.03285v1)：异构 rank 执行与 paging。

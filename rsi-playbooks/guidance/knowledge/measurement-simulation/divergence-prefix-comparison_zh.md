<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 诊断生成分歧：固定前缀分布与任务质量对照

[English](divergence-prefix-comparison.md) | 简体中文 · [推理知识](../README_zh.md)

## 现象与动机

内核变化可以改变 logits 而不改变 token，也可能用很小的扰动交换近似并列 token，让后续自回归生成进入不同上下文。这两者都不能单独判断答案质量。诊断需要区分局部数值保真、序列一致和任务准确率，不把可见文本不同当成一种失败。

## 原理与分析

浮点加法不满足结合律，batch、归约顺序和精度会改变结果，固定 seed 也不消除这些变化。设 top logit 间隔 m=z₁−z₂>0，每元素误差上界为 ε，则 m>2ε 足以保持 argmax；RMSE 不是这个上界。解释性算例中，10.000 和 9.999 分别变化 −0.0006、+0.0006 就会换序。

相同前缀下，`KL(p||q)=Σpᵢ log(pᵢ/qᵢ)` 衡量参考概率加权的分布变化，`TV=½Σ|pᵢ−qᵢ|` 为总变差。一旦生成 token 不同，后续差异就包含上下文漂移。共同 logit 偏移不改变 softmax，因此可用中心化 logit RMSE 排除它。

## 设计

用 teacher forcing 向两端提供相同 token-ID 前缀，保持词表映射、模板和特殊 token 规则一致。保留完整词表和逐位置指标；top-k 无法得到完整 KL，解码文本作为 key 也会碰撞。

另行比较原生 greedy ID 序列与首次分歧，再用同一评分规则配对任务得分。重复未修改参考实现了解自身波动。vLLM batch invariance 可在文档规定的 beta 硬件／模型／TP 范围内作诊断对照，但内核选择和性能成本不同。

## 实现

每个固定前缀位置计算：

```text
lp = reference_logits − logsumexp(reference_logits)
lq = candidate_logits − logsumexp(candidate_logits)
p = exp(lp); q = exp(lq)
KL = sum(p[i] * (lp[i]−lq[i]) for i where p[i] > 0)
TV = 0.5 * sum(abs(p−q))
centered_RMSE = sqrt(mean(((lp−mean(lp))−(lq−mean(lq)))**2))
top1 = argmax(lp) == argmax(lq)
topk_overlap = size(topk(lp) intersect topk(lq)) / k
保留参考 top 两项间隔与语料 token 概率变化
```

使用稳定 log-softmax 和完整词表。参考质量非零而候选概率为零时，KL 为无穷，不能通过截断隐藏；序列化概率先核对覆盖和质量，再归一化舍入误差。Foretoken 对照客户端提供 teacher forcing、token-ID key、完整向量指标和原生 greedy ID。出现 greedy 分歧时重评分歧前共同前缀，再由配对任务评估确定正确率是否变化。Issue #51187 报告 logprob 波动而输出 ID 不变，体现两者区别。

## 影响与效果

固定前缀将数值变化与自回归上下文漂移分离，间隔检查区分近似并列翻转与较大局部分布偏移。原生 ID 直接揭示 EOS 和序列一致，避免重分词伪差异。配对任务评分区分另一种正确答案与真实质量损失。各对照分别形成结论：分布接近不等于逐 bit 复现，序列不同不等于准确率下降。

## 参考

- [PyTorch 2.9.0 数值精度](https://github.com/pytorch/pytorch/blob/v2.9.0/docs/source/notes/numerical_accuracy.rst)：batch、归约顺序、后端精度及非逐 bit 保证。
- [vLLM batch invariance](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/batch_invariance.md)：引用版本的 beta 范围和取舍。[Issue #51187](https://github.com/vllm-project/vllm/issues/51187)的复现报告明确区分 logprob 波动与 token 分歧。
- [Foretoken 分布对照](../../../../benchmarks/docs/eval/distribution-comparison_zh.md)及[客户端](../../../../benchmarks/src/benchmarks/integrations/distributions.py)：`e6ae3df6` 的 teacher forcing、原生 greedy ID、完整概率向量和指标公式。

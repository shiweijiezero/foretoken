<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 诊断模拟失配：固定批次与到达流量对照回放

[English](simulation-replay-calibration.md) | 简体中文 · [推理知识](../README_zh.md)

## 现象与动机

模拟器的内核时长预测可以准确，请求延迟却因调度、到达或 KV 释放差异而错误。接近容量时，小时间误差会改变未来 batch 和排队。用预测排列服务配置前，应分开校准运行时成本和状态转换失配。

## 原理与分析

Vidur 按 token、sequence 和通信划分工作，基于剖析成本与随机森林插值估计。Prefill 长度 lᵢ 的等效长度 `√(Σlᵢ²)` 保留 attention 平方工作量；decode 在被评估内核假设下使用总体 KV 读取。相同总量仍可能隐藏长度倾斜、分片数、分派或专家不均。

固定批次下，每轮误差 δ 在 k 轮后可近似累积 kδ；有到达流量时还会改变准入、组批和 KV 释放，因此端到端误差不是固定偏移。CPU 开销须按真实 CPU／设备依赖建模，遗漏它或把重叠工作全串行相加都不正确。

## 设计

分离两种回放。固定实际 batch 描述比较运行时间估计，排除调度反馈；同一到达负载与策略的回放揭示准入、内存和完成顺序差异。阶段、graph 模式、dtype、上下文分布、内核和并行拓扑共同定义校准范围。

特征跟随机制：MoE 需要专家负载，推测解码需要接受相关转换，混合注意力需要自身持久状态。当前路径若没有消费这些区别，多加样本也不能弥补。

## 实现

校准与事件回放过程如下：

```text
用训练形状的实测结果拟合算子成本
对未用于拟合的实际 batch b：
    predicted[b] = 按依赖组合算子成本
    residual[b] = predicted[b]−measured[b]
    按阶段、形状、上下文倾斜与 graph 模式分组误差
初始化请求状态、KV 容量与事件优先队列
加入记录的到达事件
事件仍存在时：
    推进到下一事件
    处理到达/完成，更新等待队列、请求进度和 KV
    引擎空闲时：
        按建模的准入/调度策略组批
        batch 非空时估计时长，加入完成事件
比较组批成员、KV 占用和完成顺序
在相同总体上比较预测与实测延迟分位数
```

分位数 q 的相对误差为 `100*(predicted_q−measured_q)/measured_q`，不是逐请求相对误差的分位数。Vidur 在公布支持范围内提供 profiling、估计器和事件调度；可选 CPU overhead profiling 依赖实现。模拟的引擎时间戳与 Foretoken 客户端 chunk 时间属于不同边界。

## 影响与效果

Vidur MLSys 2024 图 3–4 中，静态 P95 的最大绝对相对差异为 3.33%，动态中位数／P95 结果中为 8.50%。静态使用不含调度等待、按输出长度归一化的执行时间；动态使用同样归一化的端到端延迟，到达为实测容量 85% 的泊松流量。各项以对应实测分位数为分母。实验覆盖 LLaMA2-7B／70B、InternLM-20B 与 Qwen-72B，使用四张 80 GB A100 或 H100、两两 NVLink 互连的 Azure VM，运行优化的 vLLM fork，负载为 Chat-1M、Arxiv-4K 和 BWB-4K。InternLM 使用 TP2，70B／72B 模型使用 TP4；论文未注明数值精度。上述数字是图中各对照的最大差异，不代表原始 TTFT／ITL 或过载误差。

固定 batch 误差识别成本／特征失配；时长准确而到达回放组批不同，指向调度或内存语义。模拟降低搜索成本，真实服务执行确定性能和质量。

## 参考

- [Vidur，MLSys 2024](https://www.microsoft.com/en-us/research/wp-content/uploads/2024/05/vidur_mlsys24.pdf)，§§3–7：特征、估计器、评估总体与容量附近的敏感性；[arXiv 记录](https://arxiv.org/abs/2405.05465)给出论文及版本。
- [Vidur README](https://github.com/microsoft/vidur/blob/abae7f63aa857300f5cdc6f5e0d27860cd24721b/README.md)和[剖析说明](https://github.com/microsoft/vidur/blob/abae7f63aa857300f5cdc6f5e0d27860cd24721b/docs/profiling.md)：引用版本的支持范围、canary、剖析前提与 CPU 文档缺口。
- [Foretoken 计时定义](../../../../benchmarks/docs/metrics_zh.md)：`e6ae3df6` 的实现；模拟的引擎时间戳不能直接等同客户端 chunk 计时。

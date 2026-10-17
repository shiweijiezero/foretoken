<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 融合基数选择与采样，减少全词表处理

[English](sampling-radix-selection.md) | 简体中文

## 现象与动机

模型前向加速后，大词表过滤和采样可能主导后处理；推测验证还会增加要处理的行数。完整排序和全词表中间张量，可能比真正需要的小候选集合昂贵得多。

## 原理与分析

Top-k 只需要确定保留集合，不需要排好全部 V 个词表项。Top-p 再在指定分布中寻找累计概率边界。将后续阶段限制到紧凑候选集合，可减少反复处理整行。

过滤顺序影响语义。例如 `[0.5, 0.3, 0.2]`、`k=2`、`p=0.6`：先 top-k 后剩余质量为 0.8，第一项已超过 `0.6×0.8=0.48`；若先 top-p，则原分布需要前两项。融合要保持指定顺序、相等值规则与随机数分配。

## 设计

FlashInfer CAKE 先以 cluster 范围的 radix selection 形成紧凑 slab，再执行 top-p 和逆 CDF 采样。分布式共享内存合并线程块间的选择信息。小 k 可以把尾部工作融合进选择内核；较大情形使用独立阶段，并可通过 dependent launch 重叠准备工作。

专用路径要求连续 float32 概率行和受支持的 Hopper 及更新 NVIDIA 设备；不适用输入转入已有确定性采样器。Greedy 是独立 argmax 路径，其他消费者无需概率时可省去过滤。

## 实现

每行概率执行：

```text
通过 radix 多轮选择精确 top-k
将选中概率和 token_id 写入紧凑 slab
按概率降序、token_id 升序排列 slab
保留累计质量达到 top_p × top-k 总质量的最短前缀
归一化后使用 Philox 随机数进行采样
```

接口支持最多 1,024 项的 slab，规定确定性的边界相等值处理和定点累计概率。Routing 查询返回专用实现或 fallback。惩罚项、约束、logits 转概率与必要复制位于核心流程周围，同样计入总成本。

## 影响与效果

专用路径把末段排序和累计概率处理限制在最多 1,024 个候选，而不是整个词表。满足条件且 `top_k_max ≤ 64` 时，可以使用一个融合内核，代替文档中的两阶段路径。

减少的是启动次数和全词表中间流量。额外转换或 float32 复制可能抵消节省，小 batch 尤其明显。[推测解码](../speculative-decoding/)中的实际价值是降低每个已提交 token 的采样成本，而不只是每行候选的成本。

## 参考

- [CAKE 采样文档](https://docs.flashinfer.ai/api/cake_sampling.html)：支持输入、routing 查询与融合条件。
- [FlashInfer 实现](https://github.com/flashinfer-ai/flashinfer/blob/d6dd9f336dcd9e4820b1583d20077804fe61147c/flashinfer/cake_sampling.py)：radix 选择、相等值、RNG 和 fallback。
- [FlashInfer sampling API](https://docs.flashinfer.ai/api/sampling.html)：其他过滤与采样路径。

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少多模态填充与等待：兼容性感知组批

[English](multimodal-shape-batching.md) | 简体中文

## 现象与动机

请求数不能表达多模态工作量。图片 patch 数不同，视频 latent 尺寸、时长、guidance、LoRA 也可能不同。FIFO 队头不兼容时，后面可一起执行的请求可能仍各自运行。形状感知组批减少 padding 和闲置，但不改变输入，也不让低频形状长期等候。

## 原理与分析

对 `n` 行填充张量，token 数为 `V_i` 时，利用率可示意为 `U=ΣV_i/(n*max V_i)`。256 与 1024 两行得到 `U=0.625`。Packing 能省掉填充，但必须用序列边界和 mask 防止跨项 attention；独立 attention 的计算按 `ΣV_i²` 增长，而非 `(ΣV_i)²`。

扩散有不同状态边界：每请求拥有自己的 latent、solver step、随机状态、conditioning 和 guidance。仅 kernel 形状一致，不代表 scheduler 或 VAE 兼容。调整队列顺序能改善批次，也可能增加其他请求等待。

## 设计

按 pipeline 的真实规则分组：分辨率和 latent 形状、guidance、输出数、LoRA 身份及 scale、执行模式。等待约束独立于设备容量，使低频形状仍能发车。请求级组批执行完整的兼容 wave；逐步组批在去噪迭代间加入请求，并保留逐请求状态。

引用 Omni 版本将不同 LoRA／scale 拆批，Helios 限单请求，MiniMax H3 packed 路径要求 FlashAttention；step execution 不支持扩散缓存 backend。`max_num_seqs` 只约束并发，并不能赋予 pipeline 逐步执行能力。

## 实现

兼容性感知组批可写为：

```text
锚点 = 队列中最老的就绪请求
key = pipeline.兼容性键(锚点)
批次 = [锚点]
扫描后续就绪请求直到容量用满:
    若pipeline.兼容(key, 请求): 加入批次
按支持的布局打包张量，保留offset、mask与位置
执行完整pipeline wave或一个受支持去噪步
逐请求更新solver和随机状态
按原请求及输入位置恢复输出
保留未完成状态，释放已完成请求
```

调度器决定批次成员，packed kernel 保证布局。使用原公开权重和 processor，不重训；降低分辨率或缩短视频不属于组批收益。BF16/FP16 对齐、量化内核和 graph capture 仍有 backend 特定约束。

## 影响与效果

Packing 减少填充元素，兼容性选取减少“队头邻居不兼容”造成的单请求 wave；两者都不消除凑批等待或下游 VAE 不兼容。0.625 是填充利用率示例，不是实测 1.6 倍服务加速。

引用 Omni 的 MiniMax H3 文档明确说已支持的 packed 组批没有改善测得吞吐，因此可用路径不等于更快服务。集中热门形状可能提高利用率、拖慢稀有形状，按年龄发车可保留进展。输入和 sampler 状态不变时，调度不主动改变质量，但数值顺序和 backend 路径仍可能影响复现性。

## 参考

- [Qwen2.5-VL，arXiv:2502.13923v1](https://arxiv.org/html/2502.13923v1)：动态形状与训练样本 packing，作为模型背景。
- [Omni 扩散执行模式](https://docs.vllm.ai/projects/vllm-omni/en/latest/user_guide/diffusion/execution_modes/)与[固定源码](https://github.com/vllm-project/vllm-omni/blob/37f094a388ba1c79f83c129fda39797c1ebfc2a4/docs/user_guide/diffusion/execution_modes.md)：当前兼容性与限制；main `37f094a` 日期为 2026-10-13，release `v0.30.0` 为 2026-09-25。
- [EAServe，arXiv:2609.31551v1](https://arxiv.org/html/2609.31551v1)：近期编码组批时机研究，论文没有证明其实现了专用异构形状 packing。

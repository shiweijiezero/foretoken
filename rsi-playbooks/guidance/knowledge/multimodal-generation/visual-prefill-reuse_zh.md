<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少视觉预填充：编码缓存与选择性 KV 复用

[English](visual-prefill-reuse.md) | 简体中文

## 现象与动机

对同一图片反复提问，会重复视觉编码和语言模型预填充。两类复用条件不同：embedding 依赖图片处理与编码权重，decoder KV 还依赖前文和位置。VLCache 用选择性重算减少上下文化视觉预填充，不等于引擎已有的精确编码缓存。

## 原理与分析

可将两种量写为 `E(image)` 与 `KV_l(image, preceding_text, positions)`。处理后的图片不变时，前者可精确复用；问题或前置指令改变后复用后者，通常属于近似，即使仍是同一图片。

视觉尺寸放大预填充成本。Qwen2.5-VL 采用 14 像素空间 patch，将相邻四个特征合并，语言侧视觉 token 约为 `HW/784`。448×448 对应 256 个，高宽同时翻倍后是 1024 个。边界标记和时间分组由 processor 决定。视觉前缀越大，decoder 侧复用越可能比单纯编码命中重要。

## 设计

首次图片请求保存 embedding 和各层施加旋转位置编码之前的视觉 KV。再次精确命中时读取状态，按当前位置处理；每层重算视觉 token 的一段前缀，其余复用。小型校准集决定分层比例，并约束比例随层深不增加。

这是精确图片查找加近似跨上下文状态复用，不是不同图片间的相似度匹配。VLCache 描述 Tair KVCache Store 与 SGLang 集成，未提供作者公开仓库。vLLM 现有编码缓存负责精确 embedding 路径，不代表已经实现这种上下文 KV 算法。

## 实现

选择性预填充流程为：

```text
图片 = checkpoint_processor(输入图片)
条目 = 按精确图片及编码器/模型版本查找
若未命中: 编码图片并计算保存pre-RoPE视觉KV
将缓存embedding绑定到当前图文序列
遍历decoder层l:
    m = 校准得到的前缀重算数(l, 视觉token数)
    按当前上下文计算前m个视觉token的KV
    对其余视觉token复用缓存pre-RoPE KV
    施加当前位置编码，用当前新计算的query执行attention
生成答案，保留图片条目供后续使用
```

公开权重与 processor 配套使用；校准选择复用比例，不训练新权重。论文覆盖 Qwen2.5-VL、Qwen3-VL。编码塔 adapter、图片顺序或 processor 改变会影响条目有效性；BF16/FP16 不会使跨上下文 KV 精确。

## 影响与效果

VLCache v1 对重复图片测 TTFT，文本为 20 token，图片最多 20K token。H20-3e 上，Qwen3-VL-8B、32B 在 20K 图片 token 时分别报告最高 1.88 倍、3.94 倍加速。8B 动态重算平均比例为 3.5%，平均基准准确率为 74.49，完整重算为 74.48。

质量并非处处一致：Qwen2.5-VL-7B 动态重算 10% 时平均分 64.72，完整重算为 65.37。编码命中省 ViT/projector，选择性 KV 复用进一步省语言模型视觉预填充，代价是缓存存储和上下文近似。细小文字与局部问题可能比总平均更敏感。

## 参考

- [VLCache，arXiv:2512.12977v1](https://arxiv.org/html/2512.12977v1)：2025 年 12 月论文的方法与实验，未找到作者代码链接。
- [Qwen2.5-VL 报告，arXiv:2502.13923v1](https://arxiv.org/html/2502.13923v1)：processor、空间合并、训练和权重发布入口。
- [vLLM 编码缓存实现](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/encoder_cache_manager.py)：`6849ca6`；精确 embedding 缓存已经实现。

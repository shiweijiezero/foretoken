<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过选择性重算复用非前缀文档 KV

[English](document-kv-recompute.md) | 简体中文

## 现象与动机

在文档前插入不同内容，再复用它的 KV，不是普通前缀缓存。即使文档 token 完全相同，因果上下文也变了。CacheBlend 复用多数条目并选择部分 token 重算；位置搬移与语义修正必须分别理解。

## 原理与分析

假设 B 的缓存是在单独处理 B 时生成，新输入却是 `A | B | question`。B 的初始嵌入除位置外可以相同，但较高层隐藏状态理应受 A 影响。把旧 key 旋转到新位置，只修复位置约定，并未补上对 A 的依赖。

采用相应 RoPE 约定时，`k_p = R(p) k` 移动 `delta` 后有 `R(delta) k_p = R(p + delta) k`。value 通常不做这一旋转。不同缩放方式、只旋转部分维度及 MLA 都需要明确几何信息，不能把通用旋转应用到所有缓存通道。

例如 A 的指令改变了 B 中一句话的解释方式。只重算最后的问题，可能改善问题到文档的 attention，却仍保留 B 的陈旧表示。重算一些 B 的 token 可以传播更多信息，但不保证与完整 prefill 相同。因此，跨文档推理和冲突指令比孤立事实检索更容易受陈旧上下文影响。

## 设计

将输入组织为分别分词的文档片段，记录缓存位置与目标位置。先确认全部 rank/read-group 对象可用，再稀疏读取；按模型的 RoPE 约定移动 key，之后由引擎逐层修复上下文相关状态。读取和位置修正提供可复用数据，选择性重算补充缺失的跨文档交互。

在配置的检查层重新选择 token，而不是为整篇文档固定修复掩码。key 变化较大的 token 获得计算机会，同时保持原序列顺序，供因果 attention 使用。最终问题与新加入的 token 仍需执行模型。

## 实现

引用版本的 `blender.py` 执行选择与替换，MP 的 `lookup.py`、`retrieve.py`、`rope.py` 负责匹配、稀疏读取与位置搬移。核心过程如下：

1. 拼接片段 token ID 与分隔符；直接拼字符串可能改变边界 token。
2. 读取当前层的缓存 K/V，对移动片段仅做 key 的 re-RoPE。
3. 为活跃 token 计算新 Q/K/V。在检查层计算 `score[t] = sum((K_new[t].float() - K_cached[t].float())**2)`。
4. 按重算比例取 top-k 索引，恢复序列顺序，替换对应缓存 K/V。
5. 让选中 query 对完整因果 K/V 做 attention，将更新隐藏状态传给后续层，逐层继续。

旧进程内实现与 MP 读取是不同路径，当前指南推荐 MP。Mooncake 搬运、Dynamo 分层加载和 vLLM 普通前缀缓存不承担这种语义修复。

## 影响与效果

CacheBlend 主实验相对完整 KV 重算将 TTFT 降低 2.2–3.3 倍，F1/ROUGE-L 下降在 0.02 以内。模型为 Mistral-7B、Yi-34B、Llama-70B，后两者使用 8-bit 权重。平台配备两张 A40、128 GB RAM 与实测 4.8 GB/s 的 NVMe；较小模型使用一张 GPU，Llama-70B 使用两张。主比较在 2WikiMQA、Musique、SAMSum、MultiNews 上为每个请求检索六个各 512 token 的片段，默认重算比例为 15%。

收益来自用缓存读取和局部计算替代完整文档 prefill。未选中的状态仍近似旧上下文，因此重算比例是在计算成本与跨文档依赖修复程度之间取舍。

## 参考

- [CacheBlend 原始方法，2024 年 5 月](https://arxiv.org/html/2405.16444v1)
- [进程内 blender](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/lmcache/v1/compute/blend/blender.py)
- [MP 模块设计](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/docs/design/v1/multiprocess/modules/blend.md)
- [当前旧模式指南](https://docs.lmcache.ai/kv_cache_optimizations/blending.html)

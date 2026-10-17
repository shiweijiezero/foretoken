<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 多模型共享 GPU 容量：按需伸缩物理内存

[English](model-memory-ballooning.md) | 简体中文

## 现象与动机

不同完整模型通常不能共享一次基础 GEMM 或一组 KV。固定 GPU 分区在各模型错峰时浪费内存；反复换出全部权重又有激活成本。Prism 用 memory ballooning 结合空间与时间共享，公开 kvcached driver 提供弹性物理 KV 分配。

## 原理与分析

为各 engine 的 KV 预留稳定虚拟地址，仅在 token block 需要时映射物理页。空页可回收，不改变引擎或 graph 使用的逻辑地址。不同模型共享设备容量，不共享 KV 内容或布局语义。

物理容量约束为 `Σ_j(W_j+K_j+A_j)+R≤C`，分别计驻留权重、物理 KV、activation/workspace、runtime 预留和设备容量；虚拟地址预留不是另一份物理占用。错峰让一个模型随另一个收缩而扩张，同时峰值仍需要足够物理容量。

## 设计

Balloon driver 协调权重与 KV。低负载驻留模型回收闲置 KV，空闲模型驱逐释放权重，可重用 engine pool 保留初始化和分布式 context。需求互补放置与本地 slack 仲裁决定哪些模型执行，避免无控制争用。

Prism 的快速加载将 tensor 分到节点内多 GPU，并行传输后通过 NVLink 汇聚，采用有界流式缓冲。它降低激活成本，是与弹性 KV 不同的职责。研究原型、终版论文、独立版本 kvcached 的 graph／prefix-cache 配置并不完全相同。

## 实现

物理内存随模型活动变化：

```text
为各engine预留稳定虚拟KV区间
token block分配时确定布局兼容的页几何
取得物理页，映射到对应engine区间
使用各自权重、KV和位置执行模型
释放block时只回收不再包含活跃block的页
其他模型需要容量时收缩未使用KV
必须移除闲置权重时，等待模型停用后驱逐
通过engine pool及并行加载路径重新激活
保持虚拟地址，不替换为其他模型的KV
```

各模型使用自己的公开 checkpoint 和 tokenizer，不训练新模型。引用 kvcached 0.1.6 README 测试 vLLM 至 0.30.0、SGLang 至 0.5.20，支持 hybrid attention 和 AMD/ROCm；AMD 默认 non-contiguous KV。过大 block 自动扩大页，dtype、TP、hybrid state 几何决定映射要求。

## 影响与效果

弹性分配去掉闲置物理 KV 预留，在互补流量下扩大有效容量，同时保持逻辑 tensor 地址。Engine 复用和并行加载减少重新激活工作，但不消除权重字节或全部启动等待。Prism 终版用四节点、最多 32 张 H100-80GB，覆盖 58 个 1B–70B 模型。TTFT SLO 倍率 5、TPOT 倍率 2 时，16 GPU 即达到两项 99% 合格率；MuxServe++ 需要 20 GPU，且 TTFT 倍率至少 30 才达到 99%。这些倍率以各模型独占 GPU 的 P95 延迟为基准，并非毫秒。收益包含 ballooning、放置与仲裁，不能全归于 allocator。

代价包括页映射／回收、block 占用碎片、权重传输和激活干扰。虚拟地址不能解决同时峰值。ServerlessLLM 提供优化加载的 checkpoint、pinned buffer、direct I/O 与 token replay 迁移：replay 用计算重建状态，直接 KV 迁移消耗带宽。加载和内存弹性都不代表不同模型能够共享计算。

## 参考

- [Prism，OSDI 2026](https://www.usenix.org/conference/osdi26/presentation/yu-shan)及[终版 PDF](https://www.usenix.org/system/files/osdi26-yu-shan.pdf)：终版方法与实验，包括 ballooning、engine 复用和加载；2026 年 7 月发表。
- [kvcached `94d117c`](https://github.com/ovg-project/kvcached/tree/94d117c1e2d6e6c190223e1de35d38e1882c7acb)：README，保留已有插件支持和版本/布局限制。
- [Prism research prototype](https://github.com/Multi-LLM/prism-research)：执行示例与原型范围，不代替当前 driver 或终版论文。
- [ServerlessLLM 原方法，arXiv:2401.14351v1](https://arxiv.org/html/2401.14351v1)与[当前仓库](https://github.com/ServerlessLLM/ServerlessLLM)：加载和迁移机制。

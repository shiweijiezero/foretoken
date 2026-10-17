<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 满足适配器请求期限：按 rank 与剩余时间调度

[English](adapter-deadline-scheduling.md) | 简体中文

## 现象与动机

共享基础模型不代表 adapter 成本相等。rank 128 可能比 rank 8 消耗更多计算与内存，首 token 和逐 token 期限不同的租户也不能只看总平均。LoRAServe 负责分布式 rank 感知放置与路由，HALO 负责 worker 调度与解码执行重叠。

## 原理与分析

LoRAServe 用近期流量估计 token 需求，按 SLO 下预先测得的 rank-specific 容量归一化，形成各 rank 预算，再装入服务器分配。每 adapter 的 routing fraction 总和为一。相近 rank 集中减少异构批次低效，减少分配变化则降低迁移。

HALO 计算到下一期限的 slack：首 token 前按 TTFT，之后按 TBT。输出队列已有生成 token 时，请求可以暂时不参加一次 decode。另一机制让 base 与 LoRA 算子并行消费同一层输入，再合并结果。SM 分区缩短解码计算，输出缓冲则改变客户端交付时机。

## 设计

分布式放置与 worker 执行分别负责不同决策。放置维护 rank 容量和需求分配；worker 优先处理紧急 prefill/decode，利用缓冲 token 余量延后部分 decode。输出队列保留顺序，按交付时间释放。

HALO 实测搜索 base／adapter 的 SM 配比，不假定平分；重叠针对 decode，不改善同一机制下的 prefill。两者采用兼容基础权重与已训练 LoRA，不改变 target module、rank、scale、tokenizer。LoRAServe 的 A100 环境使用 InfiniBand/GPUDirect RDMA 获取 adapter；HALO 依赖 GPU/runtime 的 SM 控制。

## 实现

两个层次的核心过程可概括为：

```text
按近期到达估计各adapter的token需求
用对应rank的SLO容量归一化需求
将预算装入服务器，尽量保留原分配
按归一化的服务器份额路由每个adapter
worker迭代时计算各请求到下一token期限的slack
在工作预算内选紧急prefill与decode
若缓冲输出留下足够slack，暂缓该请求decode
按实测SM配比并行base与LoRA解码计算
合并更新，将token入队并有序释放
```

这是两个论文机制的概念组合，不是已经发布的联合 scheduler。引用版本均未提供作者代码链接。设备驻留和引擎组批限制仍约束所选工作，紧急期限不能代替权重就绪。

## 影响与效果

LoRAServe 在 A100 服务器、Llama 模型及 rank 8–128 adapter 上，报告受服务约束的吞吐最高比 S-LoRA 提高 2 倍、比 Toppings 提高 20%。多数实验用四台服务器，多个对照采用 P95 TTFT 10 s 目标。其动机实验中，rank 8 与 rank 128 共置使前者 P95 TTFT 增加 84%。

HALO 用 A100 80GB 运行 Llama3-8B／Qwen2.5-7B，34B／32B 模型用四张 A100。Base–LoRA 并行平均改善 decode 8.3%、最高 14.7%，不是同样的 prefill 收益。指定场景下，完整多 SLO 系统相对 QoServe、VTC-R、VTC-T、SLoRA 的约束吞吐提高 19.5–29.5%，其中还含调度与缓冲。Rank 分组增加等待的可能，优先紧急流量也会占用宽松与低频租户容量。

## 参考

- [LoRAServe，arXiv:2511.22880v1](https://arxiv.org/html/2511.22880v1)：算法、远端传输和负载；该版本承诺接受后发布代码与 trace，但未提供作者仓库。
- [HALO，arXiv:2610.04956v1](https://arxiv.org/html/2610.04956v1)：2026-10-04 提交，slack 调度、SM 搜索和输出队列；此版本未找到作者代码链接。
- [SGLang LoRA 文档](https://docs.sglang.io/advanced_features/lora.html)：已有多 adapter 组批与 runtime 限制；main 版本为 `e9400c5`。

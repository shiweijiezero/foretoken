<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 提高专家批次利用率：Attention–FFN 分离与工作汇聚

[English](expert-batching-afd.md) | 简体中文

## 现象与动机

稀疏 MoE decode 每个专家只得到批次的一部分，而每个共置 attention 副本还在请求 KV 旁保存专家权重。Attention–FFN 分离（AFD）汇聚多个 attention worker 的 token，形成更大专家批，并从 attention 侧移除专家权重。目标是摊销专家权重读取、增加 KV 驻留。

## 原理与分析

批大小 `B`、top-k、专家数 `E` 均衡时，每专家约得到 `B*k/E` token。`B=256`、`k=8`、`E=128` 时为 16，汇聚四批后为 64；实际偏斜仍让最忙专家成为瓶颈。

每个 MoE 层都有 attention→dispatch→FFN→combine 依赖。微批让一批 attention 与另一批专家／通信重叠。关键路径由两类计算配比、dispatch/combine 和同步决定。增加微批能隐藏等待，也缩小 GEMM、增加 padding；不能把串行时间简单除以批数。

## 设计

FastAFD coordinator 生成对齐的 attention-group／expert-group 计划。空 attention lane 用 dummy batch 保持 collective 顺序，共用 dispatch bucket 保持通信和 graph 形状。Attention worker 管理 KV page，prefill 按 token 工作拆分、decode 按请求数拆分；expert worker 使用 event 编排 dispatch→expert→combine，或独立的持久化 MegaMoE 路径。

完成时释放 KV，只输出真实请求。FastAFD 是独立 `minisgl` runtime，引用 scheduler 要求 naive KV cache。Foretoken E/P/D 是请求阶段编排，不是逐层 AFD。设计空间论文引用的 vLLM PR #29772 已关闭且无 merge commit。

## 实现

一次协调迭代的核心为：

```text
coordinator选择真实批次及统一dispatch容量
空attention lane加入dummy batch
attention worker分配KV并生成有序微批
按公共计划遍历层和微批:
    用请求本地KV执行attention
    将路由activation dispatch到专家worker
    执行分组FFN，同时其他attention继续
    combine专家输出，在依赖使用前通知完成
完成真实请求并回收page，忽略dummy输出
```

非持久化路径使用 CUDA event 与可捕获 graph 形状，持久化 MegaMoE 则有独立流程。使用兼容 MoE 权重，无需新训练。FastAFD 在 GB200 NVL72 运行 FP8 Qwen3-235B-A22B、MiniMax-M2.5；BF16 MegaScale-Infer 是另一工作配置。

## 影响与效果

FastAFD 在单个 GB200 NVLink/NVSwitch 域上，相对调优 colocated vLLM，每 GPU 稳态 decode 吞吐提高 1.35–1.45 倍。Qwen3-235B 的 8K／16K 上下文为 1.41／1.44 倍，MiniMax-M2.5 为 1.45／1.35 倍。FP8 测量使用两个微批、约 0.85 KV 利用率；分母包含所有 attention 与 FFN GPU，也包含不驻留请求的专家 GPU。结果从 prefill 完成后开始，不包含在线到达效应。

重要收益是 attention 侧 KV 容量扩大，不只是专家 kernel 更快。MegaScale-Infer 的三／四微批依赖不同计算配比和通信成本。B200 设计空间研究用后端测量加 AstraSim 网络模型，其严格 SLO 前沿属于建模结果，不是 NVL72 服务实测。逐层通信、偏斜、dummy padding 和专家小批仍有成本。

## 参考

- [FastAFD 技术报告](https://haoailab.com/blogs/fastafd/)
- [MegaScale-Infer 论文](https://arxiv.org/html/2504.02263)
- [设计空间论文](https://arxiv.org/html/2605.28302v1)
- [coordinator](https://github.com/hao-ai-lab/FastAFD/blob/3c7161949310b6d59d6b4cf9bf997a4935c8113b/python/minisgl/afd_coordinator.py)
- [scheduler](https://github.com/hao-ai-lab/FastAFD/blob/3c7161949310b6d59d6b4cf9bf997a4935c8113b/python/minisgl/afd_scheduler.py)
- [expert worker](https://github.com/hao-ai-lab/FastAFD/blob/3c7161949310b6d59d6b4cf9bf997a4935c8113b/python/minisgl/afd_expert_worker.py)
- [vLLM PR #29772](https://github.com/vllm-project/vllm/pull/29772)
- [E/P/D 工作流](../../../../data-plane/frontend/src/server/src/runtime/workflow.rs)
- [扩缩容与放置](../autoscaling-placement/)

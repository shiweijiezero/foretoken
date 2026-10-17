<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 降低多模态首 token 延迟：自适应编码微批

[English](encoder-ttft-microbatching.md) | 简体中文

## 现象与动机

视觉 kernel 很快，首 token 延迟仍可能由预处理、编码队列、embedding 传输或下游就绪决定。等大批能提高利用率，也会消耗最老请求的等待预算。EAServe 将编码分离与自适应微批结合，避免不均匀到达始终等待固定批次凑满。

## 原理与分析

三种条件触发发车：批次达到容量；由近期到达率推导的空闲间隔出现；最老请求达到年龄上限。满批摊销启动并提高设备占用，另外两个条件避免继续等待没有价值。到达率只改变派发时机，不改变编码权重或图片分辨率。

视觉 token 数为 `V`、语言侧宽度为 `d`、每元素 `b` 字节时，传输负载约 `Vdb`。`V=4096`、`d=3584`、`b=2` 对应 28 MiB，尚未计协议开销。编码缓存能省计算，却不一定省消费端所需的远程传输。

## 设计

只收集已经完成预处理的张量。编码后继续保留请求及输入项位置，发布完成的 embedding descriptor；prefill 等待全部必要 embedding 加载完成再执行。前一批传输时，编码器可以计算下一批。

缓存将请求引用与物理驱逐分开管理。引用释放后条目可回收，不必立即删除；同轮调度再次使用的条目不能收到过时的删除通知。取消时清除引用，相关传输完成或停止后才可重用 buffer。

## 实现

核心发车与交接流程如下：

```text
预处理完成时: 入队(请求, 输入位置, 张量, 到达时间)
根据近期到达估计到达率
若批次满或达到自适应空闲间隔或最老请求超时:
    批次 = 提取兼容且就绪的输入(队列, 编码容量)
    embeddings = encoder(批次.张量)
    descriptors = 发布(embeddings, 请求与位置)
    启动传输(descriptors, prefill目标)
对所需传输均已完成的prefill请求:
    按输入顺序绑定embedding
    执行prefill
```

使用公开 VLM 权重及对应 processor，无需重训。EAServe 描述 CUDA、NIXL、libsmctrl 实现，未提供作者仓库。vLLM 编码分离已有 ExampleConnector、ECMooncakeConnector，后者使用 TCP 或 RDMA；文档音频范围为独立音频，不含带音轨视频。

## 影响与效果

EAServe 的 LLaVA 对照中，给 Dynamo 加简单 batch-8 微批，吞吐从 0.75 提高到 1.09 请求/s，平均 TTFT 从 367.4 降至 112.1 ms，但平均 TPOT 从 179 增至 411 ms。编码组批改善可以同时伴随下游逐 token 延迟恶化。

主实验使用八张 RTX 6000 Ada 48GB，LLaVA-v1.6-34B，以及 1–5 请求/s 范围的 Poisson 到达。中等 SLO 档下，完整 EAServe 图像 goodput 为 1.93 请求/s，vLLM 为 1.11、Dynamo 为 0.95。该结果包含资源分配、局部 prefill offload 与 SM 分区，不能全归因于微批。新增等待、embedding 占用、传输与下游就绪是主要代价。

## 参考

- [EAServe，arXiv:2609.31551v1](https://arxiv.org/html/2609.31551v1)：2026 年 9 月微批和传输机制，未找到作者公开代码入口。
- [vLLM 编码分离](https://docs.vllm.ai/en/latest/features/disagg_encoder/)：当前流程、connector、示例和音频限制。
- [编码缓存实现 `6849ca6`](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/encoder_cache_manager.py)：embedding 生命周期与驱逐。

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过内存感知的运行集合选择避免 KV 抢占与重算抖动

[English](kv-pressure-admission.md) | 简体中文

## 现象与动机

请求接纳时能放入 KV cache，输出增长后却可能耗尽容量。反复驱逐与重算会把 GPU 工作花在恢复进度而非生成新 token 上。内存感知选择需要保持运行集合可执行，同时避免按每个请求的最大输出预留全部空间。

## 原理与分析

两个 8K 输入、预计输出 1K 的请求起初可能都能放入；若各生成 8K，合计 footprint 接近翻倍。普通 dense attention 的粗略估计为：

`KV_bytes ≈ resident_tokens × 2 × layers × KV_heads × head_dim × element_bytes`。

实际逐 rank 分配还受 TP、MLA、混合状态组、共享前缀和块取整影响，缓存输入与未来输出的共享机会也不同。

只看当前占用会把恢复成本留到未来；预留最大输出则在多数请求提前结束时浪费容量。SOLA 在准入前预测未来峰值内存，其简化算法不采用 swapping／preemption。SCORPIO 将长度预测与延迟准入结合；低估输出会使未来运行集合超出容量。

## 设计

按当前分配和预计剩余增长选择运行集合，暂缓会造成超额占用的新请求。若实际增长仍耗尽内存，通过引擎既有抢占生命周期恢复：驱逐大而旧的请求回收更多空间，却丢失昂贵进度；驱逐短请求可能影响更多期限。

只有引擎知道哪些块能释放，并能正确恢复请求。请求层限制向高压实例新增派发；已有 KV 在完成或取消后由引擎释放。集群编排在较慢时间尺度补充容量。

## 实现

1. 按逐 rank 布局估计候选新增块，计入可复用前缀和预计输出增长。
2. 仅在选中集合满足内存范围与执行预算时接纳工作。
3. vLLM V1 调用 `allocate_slots`；失败后选择实际可释放块的运行请求。Priority 调度取最大的 `(priority, arrival_time)`，其他调度取运行列表尾部。
4. 抢占释放 KV／编码器状态，重置计算进度、清除 draft token，再放回等待队列；若本轮已安排 victim，还需归还对应预算。
5. 恢复时按保留历史重建缺失状态，并复用可用前缀。In-flight 执行与延后释放决定内存何时真正可用。

## 影响与效果

保持批次可执行能减少 KV 抖动，避免恢复工作挤占有效生成。vLLM V1 默认采用重算而非 CPU swapping。保守准入减少抢占，却可能让内存闲置；乐观长度预测能暂时提高并发，直到长输出暴露误差。Prefix caching 可以保留部分进度，因此重置已计算状态不代表历史 token 必须全部重算。

## 参考

- [vLLM scheduler，commit 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/sched/scheduler.py)：分配、`_preempt_request`、victim 选择与延后释放约束。
- [vLLM 优化指南](https://docs.vllm.ai/en/latest/configuration/optimization/)：抢占信号与现有操作选项。
- [SOLA，MLSys 2025，§5.1](https://proceedings.mlsys.org/paper_files/paper/2025/file/bc82dbfbfa43232be85b8d9838f49c3e-Paper-Conference.pdf)：批次接纳时检查未来峰值内存。
- [SCORPIO，arXiv 2505.23022v2，2026-08-26](https://arxiv.org/html/2505.23022v2)：异构目标下依赖预测的准入。

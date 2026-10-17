<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少适配器换入换出抖动：驻留感知请求派发

[English](adapter-residency-dispatch.md) | 简体中文

## 现象与动机

动态加载和 LRU 不能阻止驻留悬崖：被请求的 adapter 多于 GPU slot 后，一次必需换入就可能阻塞共享执行，连续 miss 放大其他租户尾延迟。CLIMB 协调入口与活跃集合，避免后台请求不断挤走低延迟 VIP 所需的 adapter。

## 原理与分析

四个 slot、八个被请求 adapter 时，将所有请求立即交给引擎可能反复换入换出。让未活跃后台请求留在入口，可以减少紧急工作期间暴露的加载，但后台入口等待仍属于端到端延迟。

成本取决于 rank 和 target module，不只是 slot 数。输入宽度 `h`、输出宽度 `o`、rank `r`、每元素 `b` 字节的线性层，原始 adapter 大小为 `b*r*(h+o)`。同层 rank 128 的 A/B 字节是 rank 8 的十六倍。按最大 rank padding 的池可能分配更多，每项 adapter 预留又与 KV 容量竞争。

## 设计

CLIMB `gate_rr` 保存活跃集合及各 adapter 的排队／in-flight 工作。清除空闲活跃项后，先 round-robin 激活 VIP，再用剩余容量放后台。控制条件计数的是有排队或 in-flight 工作的不同 VIP adapter，不是请求数，也不只数已驻留 VIP。达到容量时暂停后台派发，但不抹掉仍在执行的后台工作。

设备驻留仍由引擎 loader 负责。SGLang 驱逐时排除当前批次和 pinned adapter，复制 slot 权重、清空缺失 module buffer，并让 TP rank 的 UID 映射一致。替换重用 buffer 前需要等活跃工作结束。

## 实现

入口策略的核心过程为：

```text
移除没有排队及in-flight工作的活跃项
w_vip = 有排队或in-flight工作的不同VIP adapter数
允许后台 = (w_vip < 驻留容量)
按round-robin激活排队VIP，直到容量用满
若允许后台: 用空余slot激活后台adapter
否则: 移除空闲后台项，优先激活VIP
候选 = 活跃adapter中当前类别允许的排队请求
派发最早候选，并增加对应in-flight计数
完成时减少in-flight，再重新分配活跃容量
```

实际权重加载和设备驻留确认不在 `gate_rr.py` 中。CLIMB 发布策略、整理数据及依赖外部 serving harness 的 GPU wrapper。合成 rank-128 权重用于缓存压力，不代表任务质量；真实服务需另取兼容训练 LoRA。基础权重量化后，FP16/BF16 adapter 内存仍应单独计量。

## 影响与效果

CLIMB 的 ICML 发布页报告 VIP P99 TTFT 从 38.7 降至 13.1 s，吞吐保持 10.66 请求/s，VIP 引擎内延迟约 0.13 s。场景是高利用率单 GPU，活跃 adapter 超过剩余驻留 slot；发布页未列 GPU／基础模型。Artifact 用合成 rank-128 权重产生这类压力。不同紧急 adapter 的需求达到容量时，暂停后台派发减少后台引起的切换，让暖 VIP 流量不必为每个后台到达等待加载。代价是后台等待及切换期间可能闲置的容量；engine-only 尾延迟改善不等于入口等待消失。

SGLang 现有 overlap loading 能隐藏复制，也可能限制多 adapter prefill 组批。Pinning 减少驱逐却占用灵活 slot；扩大池减少 miss，直到 KV 压力限制并发上下文。CLIMB 改变顺序，不改变 adapter 语义，合成权重结果只说明驻留与排队。未使用预取浪费带宽或挤走有效状态；host staging 和 PCIe 决定 miss 暴露的执行停顿。

## 参考

- [CLIMB，ICML 2026](https://proceedings.mlr.press/v306/zhang26dg.html)：机制与 artifact 入口；[artifact](https://github.com/maxsignalll/CLIMB_artifact/tree/a96f7f3cb65b9bb86dec3d0a1f0c511705f05360) README、`policies/gate_rr.py` 版本为 2026-06-12 的 `a96f7f3`，GPU 重跑依赖外部 serving harness，合成权重用于产生驻留压力。
- [SGLang 内存池](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/lora/mem_pool.py)：commit `e9400c5` 日期为 2026-10-13；[运行文档](https://docs.sglang.io/advanced_features/lora.html)。
- [vLLM LoRA](https://docs.vllm.ai/en/latest/features/lora/)与 [TensorRT-LLM LoRA](https://nvidia.github.io/TensorRT-LLM/advanced/lora.html)：当前加载及缓存文档；TensorRT-LLM main 版本为 `31c1a3f`。

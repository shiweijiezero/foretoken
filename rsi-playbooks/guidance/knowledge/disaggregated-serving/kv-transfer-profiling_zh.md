<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 诊断 KV 交接停顿：汇聚传输剖析

[English](kv-transfer-profiling.md) | 简体中文

## 现象与动机

网卡快不代表 P/D 交接快。KV 移动包括来源查找、注册或主机中转、descriptor、shard、布局转换和完成可见性。多个 P 可能汇聚到一个 D，也可能与专家流量争链路。汇聚剖析能解释单对峰值带宽看不到的交接停顿。

## 原理与分析

普通全注意力 GQA 的逻辑负载为 `K≈2*S*L*H_KV*d*b`，分别是 token、层、KV head、head dimension 和每元素字节，系数二表示 K/V。Padding、scale、复制、封装增加字节；MLA、滑动窗口、hybrid state 要使用真实表示。

`S=32768`、`L=32`、`H_KV=8`、`d=128`、BF16 `b=2` 时为 4 GiB。理想有效带宽 50 GB/s 下，仅传输约 86 ms；每秒十个约需 43 GB/s。这是算术界限，不是实测。FP8 KV 近似减半元素字节，FP8 权重本身不改变负载。

将延迟分为 `T_X=Q_X+setup+pack+K/B_effective+unpack+completion`。有效带宽随消息及竞争变化，线速不含 setup 和排队。

## 设计

关联请求、源／目标 shard、descriptor、传输提交与完成事件。按真实 KV 布局观察 fan-in，含 TP 复制或转换；将 setup 与 payload 分开，避免把注册耗时误认为低带宽。

Producer 在传输不再读取前保留源 page，consumer 保留目标供 decode。取消或失败后要先停止访问再重用。逐层重叠减少暴露延迟，却延长 buffer 生命周期。GPU/NIC 亲和、NUMA、PCIe、rail 决定路径；host-staged TCP 相比设备 RDMA 增加 GPU↔host 复制和 pinned memory 压力。

## 实现

交接及剖析过程为：

```text
按真实KV布局和TP shard生成传输segment
分配兼容目标page，保留源引用
记录setup、注册、packing及请求标识
提交带源/目标关联的shard传输
记录逐shard完成及unpack/转换工作
只把已完成且可用状态交给decode
传输不再读源时释放源引用
取消或失败后，停止访问再重用buffer
按fan-in和请求大小汇总有效字节及暴露等待
```

NIXLBench 提供 pairwise、many-to-one、one-to-many、DRAM/VRAM、backend 和分位延迟，测传输而非实现服务重叠。Foretoken E/P/D 的 KV 使用 Mooncake RDMA 或 host-staged TCP，encoder 状态在该示例走共享存储；workflow 负责跨阶段清理。

## 影响与效果

Trace 区分传输饱和、setup、packing 和 decode 就绪停顿。增加并发只在链路或 consumer 排队主导前提高利用率；中转、重复发送和保留 page 可能吃掉快速网卡的表面优势。

AFD 设计空间论文的 450／25 GB/s 是 NVLink／InfiniBand 放置模型条件，不是可移用的实测 KV 加速。MegaScale-Infer 的 BF16 M2N 是逐层 activation 测量，不是一次 P/D KV。分块只隐藏与有效计算重叠的传输部分；必要 shard 未就绪就开始 decode，只把等待移入解码停顿。剖析增加观测开销，不改变权重和质量。

## 参考

- [NIXLBench README](https://github.com/ai-dynamo/nixl/blob/01656768741dc87922a2045feff72921e193d981/benchmark/nixlbench/README.md)
- [AFD 设计空间论文](https://arxiv.org/html/2605.28302v1)
- [MegaScale-Infer](https://arxiv.org/html/2504.02263)
- [E/P/D 部署文档](../../../../examples/feature/encoder-prefill-decode/README_zh.md)
- [执行工作流](../../../../data-plane/frontend/src/server/src/runtime/workflow.rs)
- [并行与通信](../parallelism-communication/)

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 降低恢复延迟：休眠唤醒与保留状态复用

[English](recovery-state-reuse.md) | 简体中文

## 现象与动机

恢复成本取决于什么仍然存在：检查点文件、主机权重副本、活着的内存 owner，还是请求 token 历史。“热”不是单一状态，同一 GPU 上睡眠引擎与重启进程可能走完全不同的路径。

## 原理与分析

level-1 wake 的载荷下限约为 `W/B`，W 是权重字节，B 是有效 H2D 带宽，另有分配/恢复和请求重放。level 2 还增加来源读取和转换。例如保留 40 GiB 主机权重可以省部分加载，却占用相应 RAM；内存压力可能抵消恢复收益。

## 设计

根据存活状态选择恢复路径：level 1 依赖主机权重备份，level 2 需要权重来源，IPC 复用依赖存活 daemon 与匹配放置，allocation artifact 需要张量重建元数据。请求恢复还需单独恢复兼容 KV 或重放精确 token 历史。

权重、必要 buffer 与 KV 分配就绪后，才将引擎交给调度器。新权重修订会破坏旧 KV 的语义前提。内存所有权与物理 GPU 身份也是恢复条件，不能从进程重启成功推断。

## 实现

休眠与唤醒顺序如下：

1. Level 1 将权重备份到 CPU，丢弃 KV；wake 从备份恢复权重并重建所需分配。
2. Level 2 丢弃权重／KV，但在 CPU 保留必要 buffer。先唤醒 weights、明确 reload，再唤醒 KV；只唤醒分配不会恢复已丢弃数据。
3. 请求状态通过兼容外部 KV 恢复，或先重放输入和已经生成的 token ID，再继续采样。

IPC loader 可零拷贝映射 daemon 所有的 CUDA 张量，也可克隆到引擎自有存储。模型、并行方式、角色与 GPU 身份须匹配。零拷贝要求 daemon 存活，不兼容 sleep-mode 权重卸载。Dynamo GMS 将 allocation 字节恢复到新 server，消费者按兼容分配粒度重建张量 view；client 与 sidecar 须解析到同一物理 GPU。

ServerlessLLM 请求迁移先在目标重放 token 快照，再补传切换前新生成的 token 做第二次重放，保留已产出 token，而不是重新采样。

## 影响与效果

Level-1 wake 省去存储读取和重复权重解释，但仍有 H2D 恢复，并占用主机权重内存。Level 2 释放更多内存，增加明确加载。Daemon 零拷贝省去新权重副本，但可用性绑定其存活；克隆模式用设备内存与拷贝时间换取引擎所有权。

Token 重放避免搬运完整 KV 历史，改为花费 prefill 计算。缓存语义与接入匹配时，外部 KV 可避免重放，但权重恢复本身不使所有在途请求可恢复。

## 参考

- [当前 sleep 指南](https://docs.vllm.ai/en/latest/features/sleep_mode/)
- [weight-cache IPC loader](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/weight_cache/ipc_loader.py)
- [GMS artifact](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/gpu_memory_service/v1/snapshot/weight_artifact.py)
- [ServerlessLLM 原方法](https://arxiv.org/html/2401.14351v1)

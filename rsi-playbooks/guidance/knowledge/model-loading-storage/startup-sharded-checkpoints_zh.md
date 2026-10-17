<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过 Rank 本地检查点与加载流水降低启动成本

[English](startup-sharded-checkpoints.md) | 简体中文

## 现象与动机

下载完成不等于模型就绪。启动还要解析文件、分配张量、读取并安装权重、转换成运行时表示，再准备执行。加快文件读取消除不了所有后续阶段，缓存检查点也不一定缓存了运行时表示。

## 原理与分析

检查点大小 W 时，顺序读取约为 `T_read = W/B_storage`；启动还包括安装、H2D、转换与运行时准备。真正流水化后，稳定阶段由最慢阶段决定，而非各阶段之和，但启动、排空和模型特有工作仍存在。只有实际按块重叠时才符合这一估计。

以 TP4 加载全局 40 GiB 权重为例，各 rank 读取全部权重再切片，聚合 I/O 可能远大于各自读约 10 GiB 本地表示。这只是均衡示例，复制张量、expert 分配、padding 与运行时量化都会改变数字。跳过无关张量读取与一次转换成 rank 本地检查点，解决的是不同成本。

## 设计

在重复启动路径之外准备拓扑特定检查点，保存各 TP/PP rank 实际拥有的运行时参数，过滤重复别名／view。启动时构造匹配模型，将这些张量直接复制到已准备参数。文件名表示 rank 坐标而非 TP/PP 大小，部署须保留解释这些坐标的拓扑。

分块 loader 还能将存储、主机暂存和设备拷贝流水化。分片避免无关读取与重复切片，流水隐藏暴露的阶段时间；二者互补，但都不替代编译、图捕获等执行准备。

## 实现

引用版本 `ShardedStateLoader` 的直接安装过程如下：

1. 按目标 TP/PP 分配构造模型，枚举参数存储。
2. 生成 rank 本地 safetensors 时过滤别名和重叠 view，避免重复保存同一存储。
3. 加载时只解析当前 TP rank、以及启用时当前 PP rank 对应文件。
4. 逐张量读取，复制到对应已准备参数或有效子 view，报告缺失的必需张量。
5. 完成适用的模型加载后处理与运行时准备，再提供服务。

该 loader 不负责任意运行时量化或重新分片。默认加载仍负责模型特定融合名称、切片与转换，本地 expert 过滤也受专家负载均衡要求约束。ServerlessLLM 使用对齐 GPU 专属二进制块和偏移索引组织流水。ModelExpress 是 vLLM 已有的另一条加载路径；GMS allocation artifact 与命名张量检查点需要不同消费者。

## 影响与效果

参数确实分片时，rank 本地检查点省去重复读取完整权重与模型切片。代价是准备好的表示绑定模型、dtype 和拓扑；改变拓扑需要支持的转换，不能只改文件名。复制张量与 expert 分配决定实际节省字节。

流水可以重叠各阶段，但启动仍由最慢加载阶段与最终处理限制。因此权重优化只减少进程启动到首个生成中的相应部分。Mooncake 发布元数据与 LMCache KV 持久化不消除权重安装。

## 参考

- [DefaultModelLoader](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/default_loader.py)
- [ServerlessLLM，2024 年 1 月](https://arxiv.org/html/2401.14351v1)
- [ShardedStateLoader](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/sharded_state_loader.py)
- [ModelExpress](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/modelexpress_loader.py)
- [weight artifact](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/gpu_memory_service/v1/snapshot/weight_artifact.py)

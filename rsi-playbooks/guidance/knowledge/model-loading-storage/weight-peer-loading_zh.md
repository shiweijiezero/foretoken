<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过 Peer 与共享存储复用降低权重加载成本

[English](weight-peer-loading.md) | 简体中文

## 现象与动机

热 peer 可以提供已安装权重，共享存储则可能提供检查点文件或准备好的产物，省掉的启动工作不同。字节传输兼容还需要模型身份、rank 布局和运行时解释一致，仅能连通 peer 不够。

## 原理与分析

文件缓存命中后仍需解析检查点、安装参数。运行时张量 peer 的名称、形状、dtype、分片与 backend 表示匹配时，可以避免重复转换。allocation artifact 提供原始字节，需要额外信息重建张量 view。三者都不自动恢复请求 KV 或执行状态。

例如一个 rank 需要 10 GiB 运行时权重，peer 有效 40 GiB/s 时仅载荷就需 250 ms，文件源 5 GiB/s 时需 2 s。元数据发现、注册、源同步、安装和预热另计。多个目标同时加载会分摊源带宽，还可能与源的推理竞争；不读取模型文件不意味着加速无成本。

## 设计

将源发现与数据路径分开。协调方选择兼容的实时张量状态或准备好的文件源，peer 载荷经 NIXL 直接传送，不经过中央 server。目标存储与最终处理归 loader 所有，部分写入后的恢复也在这一职责内。

兼容性包括模型修订、张量 manifest、dtype、rank 几何、运行时表示与 worker incarnation。Peer 提供权重，不提供请求 KV 或执行状态。原始 allocation artifact 需要外部张量重建信息，文件缓存仍需解析与安装。

## 实现

引用版本 ModelExpress 的 vLLM loader 按适用条件执行策略链：

1. 发现匹配 peer，取得张量 manifest，分配／注册目标存储，以 NIXL 搬运兼容张量。
2. 等待完成，补齐引擎特有权重状态；在途读取期间源分配保持有效。
3. 策略不可用或遇到 loader 可处理失败时，依次尝试满足条件的 server cache、InstantTensor、ModelStreamer、GDS 和原生加载。部分目标不能混入两次尝试的不兼容数据。
4. 没有共享存储时，文件缓存流式重建本地 snapshot，供文件消费者读取。

vLLM 原生 wrapper 将行为委托给 ModelExpress。Mooncake 引用版本已实现发布／修订元数据、lease 与 HA 恢复，但 managed reader 及后续客户端／RPC 表面不构成完整加载路径。Dynamo GMS 恢复对齐 allocation 字节，不记录张量名称、形状与 dtype。

## 影响与效果

兼容的已安装张量复用省去重复检查点解析与表示转换，热文件源主要省远端存储读取。Peer 加载增加发现、注册、同步和搬运；多个目标共享源带宽，扇出还可能与源推理竞争。

载荷很快不消除最终处理与预热。Fallback 让更快来源不可用时仍能加载，但原生加载成功与 peer 复用成功是不同路径。Loader 的来源身份与完成处理保证复用权重一致。

## 参考

- [ModelExpress 架构](https://github.com/ai-dynamo/modelexpress/blob/db0cbce97184d9135d35c7290711f3053b220b6a/docs/ARCHITECTURE.md)
- [vLLM loader](https://github.com/ai-dynamo/modelexpress/blob/db0cbce97184d9135d35c7290711f3053b220b6a/modelexpress_client/python/modelexpress/engines/vllm/loader.py)
- [原生 ModelExpress wrapper](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/modelexpress_loader.py)
- [权重管理说明](https://github.com/kvcache-ai/Mooncake/blob/342c8aeece6b4eefb76c6b8b31ccfac8dde07aa3/docs/source/design/mooncake-reshard/weight-management.md)
- [weight artifact](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/gpu_memory_service/v1/snapshot/weight_artifact.py)

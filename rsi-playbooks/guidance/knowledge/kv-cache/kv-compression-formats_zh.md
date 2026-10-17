<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按压缩目标选择 KV 量化或存储编码

[English](kv-compression-formats.md) | 简体中文

## 现象与动机

“压缩 KV”可能指低精度常驻张量，也可能指读取后先解码、再供 attention 使用的存储对象。前者可以减少运行时容量与读取量，后者主要减少存储和传输字节。混淆两者会误判容量与性能。

## 原理与分析

[KIVI，2024 年 2 月](https://arxiv.org/html/2402.02750v1)采用不对称量化：key 沿 token 方向逐通道分组，value 沿通道方向逐 token 分组。前者针对持续出现的 key 通道离群值，后者针对随 token 变化的 value 范围。每组以 `x_hat = s q + z` 重建，其中 `B` 是量化位宽，`z = min(x)`，`s = (max(x) - min(x))/(2^B - 1)`。整数、scale 和 offset 都占空间。

key 需要积累一定 token 才能组成组。KIVI 保留近期全精度残差窗口，典型组大小为 32、残差长度为 128。2-bit 有效载荷名义上只有 BF16 的八分之一，元数据、残差、对齐和工作区会降低实际节省，短历史尤其明显。

不计实际重叠时，存储编码加载成本为 `T_fetch(D_compressed) + T_decode + T_install`，常驻张量仍可能接近未压缩大小。运行时量化改变的是 `T_read + T_dequant + T_compute`，包含打包与 scale 访问。内核低效或 attention 不是瓶颈时，表示更小也可能更慢。

## 设计

按消费者选择表示。KIVI 保留量化历史与近期全精度残差窗口，attention 同时使用两者，key 与 value 按不同轴量化。CacheGen 为上下文保存多个编码版本，读取后先重建张量再安装，让加载过程在网络字节、解码与重算成本之间选择。

vLLM 运行时 FP8 是另一种表示：校准 scale、逐张量或后端支持的逐头缩放，以及指定层保留原 dtype，共同决定 attention 读取内容。单位 scale 与校准 scale 的数值条件不同。存储编码本身不改变常驻 attention 格式。

## 实现

KIVI 的追加与 attention 算法如下：

1. 将新 K/V 追加到残差窗口。旧 key 组成 token 组后，按通道计算范围；value 逐 token 在通道组内计算范围。
2. 组内取值不同时，用 `q = round((x-z)/s)` 量化并裁剪到整数范围。常量分组使用零编码并重建为 `z`，scale 存储沿用量化器约定；打包整数、保存 scale/offset。
3. Attention 按需反量化旧状态，与近期全精度状态结合。

CacheGen 的存储算法如下：

1. 切成十 token 块，将首 token 的 K/V 编为 8-bit 锚点。
2. 按层选择精度，量化相对重建锚点的差值，再对符号做算术编码。
3. 按加载成本选择已有编码或文本重算。选择编码时，解码符号、由锚点与差值重建 K/V，再安装。

LMCache 通过 `remote_serde: "cachegen"` 接入 serializer；旧进程内配置不等于 MP 配置。vLLM NIXL 两端还要求 scale 表示兼容，支持静态或 packed scale 不代表支持动态 scale 互通。

## 影响与效果

KIVI 在一张 80 GB A100 上评测 Llama-2-7B，使用由 ShareGPT 构造、平均输入 161 token、生成 338 token 的负载。相对 FP16，残差长度为 32 或 128 的 KIVI-2 将包含权重的峰值内存降低 2.6 倍，在相近最大内存下支持最高 4 倍 batch，吞吐提高 2.35–3.47 倍。实验将 batch 增大到内存上限，收益包含容量扩大，并非相同 batch 的内核加速。

CacheGen 减少编码后的存储与传输字节，但增加解码，张量在 attention 前展开。运行时量化减少常驻字节与读取，增加打包、scale 访问和反量化。名义位宽比或熵编码本身都不能决定服务速度。

## 参考

- [KIVI，2024 年 2 月](https://arxiv.org/html/2402.02750v1)
- [CacheGen，2023 年 10 月](https://arxiv.org/html/2310.07240v1)
- [量化 KV 文档](https://docs.vllm.ai/en/latest/features/quantization/quantized_kvcache/)
- [CacheGen 指南](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/docs/source/kv_cache_optimizations/compression/cachegen.rst)
- [NIXL 兼容规则](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/nixl_connector_compatibility.md)

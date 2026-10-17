<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过 Query 与 KV 分片扩展长上下文注意力

[English](context-query-kv-sharding.md) | 简体中文

## 现象与动机

长上下文 prefill 分摊大量 query；decode 则让极少的新 query 访问大量历史。二者需要不同通信模式。都叫序列并行或上下文并行 CP，可能掩盖实现到底切分 query、head、KV 历史，还是只切分中间激活。

## 原理与分析

Ulysses 从序列分片 Q/K/V 开始，用 all-to-all 把归属转成“更长序列、更少 head”，本地计算这些 head 的 attention，再做逆向 all-to-all 恢复序列归属。简单实现受到 head 整除限制，KV head 少或 TP 已切分 head 时尤其明显。

Ring attention 保留本地 query，循环交换 KV 块，以稳定的在线 softmax 合并块，不存完整分数矩阵。Ulysses 重新分配 head，ring 分摊 KV 访问，二者组合将通信划分为两个维度。

## 设计

统一序列并行 USP 建立正交的 Ulysses 与 ring 组，二者 degree 相乘为总序列并行度，例如八 rank 使用二乘四。频繁 all-to-all 放在高速域，块计算足以隐藏传输时可让 ring 跨另一域。Zigzag 将早晚因果 query 块配对以平衡工作，同时保留原位置、RoPE 与 mask。

解码上下文并行 DCP 则在已有 TP 组中切分 KV 历史，通信 query／输出信息。一个 KV head、TP 八时，可以把八份重复历史改为分布式归属而不增加 GPU。预填充上下文并行 PCP 切 query 不一定切持久 KV：引用版本 PCP-only decode 收集新增 KV 以维持完整副本，与 DCP 组合的路径则保留复制 decode query。

## 实现

USP prefill 的过程如下：

1. 按支持的因果均衡方式重排 query 块，保留原 position ID。
2. 在 Ulysses 组内 all-to-all Q/K/V，将序列归属转换为 head 归属。
3. 在 ring 组内循环交换 K/V 块，为各有效块更新在线 softmax 最大值、归一化和与 value 累积量。
4. 归一化输出，逆向 all-to-all，撤销 query 置换。

DCP decode 则由各 owner 计算局部归一化输出 `o_r` 与对数归一化量 `lse_r`，按下式合并：

```text
L = logsumexp_r(lse_r)
o = sum_r exp(lse_r - L) * o_r
```

空分片（`lse_r = -inf`）直接跳过，不能乘上未定义局部输出。Head 整除关系、TP／KV-head 关系、分页缓存后端与推测 query 支持决定适用性。YunChang 仅前向实现本身不提供服务缓存接入，vLLM 指南仍将 prefill 方案标为开发中。

## 影响与效果

Query 分片分摊长 prefill attention，因果均衡放置减少最慢 rank 的不均衡；代价是归属转置、ring 流量与临时缓冲区。短序列的启动／reshape 开销可能超过节省计算。

DCP 减少重复 KV，扩大可用历史／请求容量，但增加 query／输出通信与 softmax 合并。容量扩大可提高吞吐，不等于单次 decode 更快。USP 原始 BF16/FP16 训练与 attention 结果属于不同于持续分页 decode 的负载。

## 参考

- [USP 论文](https://arxiv.org/abs/2405.07719)
- [YunChang a5f8f600](https://github.com/feifeibear/long-context-attention/tree/a5f8f600d435ced180a97c91c68e87ff3cfb8f89)
- [6849ca69 的 CP 指南](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/serving/context_parallel_deployment.md)
- [PCP 缓存操作](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/attention/ops/pcp.py)

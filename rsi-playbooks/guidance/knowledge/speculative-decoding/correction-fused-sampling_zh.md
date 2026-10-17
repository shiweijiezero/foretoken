<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 融合拒绝采样，降低保持目标分布的校正成本

[English](correction-fused-sampling.md) | 简体中文

## 现象与动机

目标验证之后，词表投影、过滤、残差采样、随机数、输出整理和 logprobs 仍会占用时间与内存。融合这些阶段可以降低单轮成本，无需更换 proposer 或另训验证模型。

## 原理与分析

设 p 是候选历史下的目标采样分布，q 是实际产生候选 x 的分布。以 min(1,p(x)/q(x)) 接受 x；拒绝时从归一化 max(p-q,0) 采样；全部成功时从目标分布采样 bonus。保持的是目标分布，不是同 seed 文本逐字一致。

词表 {A,B} 上若 p=(0.6,0.4)、q=(0.8,0.2)，A 以 0.75 接受，拒绝后只能生成 B，最终 P(A)=0.8×0.75=0.6。拒绝后无条件从目标重采样会多生成 A。确定性候选的 q 是点质量，而不是 argmax 前的 softmax。

## 设计

将概率处理、接受、校正和输出整理留在设备侧。每个假设因果历史下的 p 都包含温度、top-k/top-p、grammar 和惩罚；重排后的 q 必须反映真实提议过程。提前计算残差可能产生未使用结果，却省去主机同步。精确块验证采用不同概率耦合，可能保留更长前缀，但不是逐 token 拒绝后跳过错误位置。

## 实现

逐 token 规则可概括为以下概念步骤，不是引擎 API：

1. 按候选顺序，用当前假设前缀处理目标分布 p，取得实际 proposal 分布 q。
2. 以 min(1,p(x)/q(x)) 接受候选并延长前缀。
3. 首次拒绝时，从归一化 max(p-q,0) 采样校正并停止；全部接受则采样目标 bonus。
4. 提交输出前缀，恢复未提交状态。

固定版本 vLLM 先计算各位置恢复候选，再选有效输出前缀，并区分贪心 argmax 与随机路径。FlashInfer 融合链 API 接收 draft 概率 [B,K,V]、ID [B,K] 和目标概率 [B,K+1,V]。独立 accepted 计数可包含首次拒绝后的位置；emitted-draft 才是实际前缀，且不含 bonus。

## 影响与效果

融合减少中间概率流量、重复词表遍历和 kernel 启动；API 若仍物化稠密概率，就仍需 O(BKV) 存储。top-k/top-p 排序和 logprobs 也可能继续占主要成本。固定 SGLang 指南支持 CUDA/ROCm 下随机 EAGLE/EAGLE3/NEXTN 的精确块验证，要求 topk=1 和完整词表 proposal，也覆盖受支持的随机 DFlash/DSpark 路径。它增加词表归约；贪心点质量 DFlash 不会因此得到更长前缀。降低近似接受阈值不是同一优化。拒绝后的 logits 对应错误历史，不能输出；非 KV 状态恢复见[循环回放](../attention-state/state-write-replay_zh.md)。

## 参考

- [vLLM 固定采样器](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/sample/rejection_sampler.py)：预处理、点质量提议、残差选择与输出整理。
- [FlashInfer 链 API](https://docs.flashinfer.ai/generated/flashinfer.sampling.chain_speculative_sampling.html)：形状、随机数 offset 和 accepted/emitted 区别；滚动文档需匹配安装的库版本。
- [SGLang 固定指南](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/docs/docs/advanced_features/speculative_decoding.mdx)：受支持的块验证组合及词表归约成本。
- [Block Verification Accelerates Speculative Decoding](https://arxiv.org/abs/2403.10444) 定义原始算法，上面的固定引擎指南界定服务支持范围。

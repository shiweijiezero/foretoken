<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 推理知识

[English](README.md) | 简体中文

整理理解和改进推理系统所需的原理、研究与经验。条目说明机制、适用条件和依据，区分资料报告与 Foretoken 实测结果。

- [按层组推进 MoE 预填充](layered-prefill_zh.md)：在预填充与解码混合负载下，减少小 token 分块带来的专家权重重复读取。
- [减少 Triton 无用特化](triton-unused-specialization_zh.md)：在调用处统一无用的编译期布局参数，分别验证编译版本收敛与服务性能。

研究或实验带来新认识时，补充或修正条目，保留有价值的失败原因、实现位置和可复用实验。写法见[记录与学习](../meta-knowledge/05-recording-and-learning_zh.md)和[知识条目模板](../../templates/guidance-template_zh.md)。

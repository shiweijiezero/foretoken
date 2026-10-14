<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Inference Knowledge

English | [简体中文](README_zh.md)

Principles, research, and experience for understanding and improving inference systems. Entries explain mechanisms, applicable conditions, and evidence, distinguishing reported research from measurements in Foretoken.

- [Layer-group prefill for MoE](layered-prefill.md): reduce repeated expert-weight reads from small token chunks under mixed prefill/decode traffic.
- [Avoid unused Triton specializations](triton-unused-specialization.md): normalize irrelevant compile-time layout parameters and measure compilation reduction separately from serving gains.

Add or revise an entry when research or experiments improve the understanding of a topic. Include useful failures, implementation locations, and experiments that later work can reuse. See [recording and learning](../meta-knowledge/05-recording-and-learning.md) and the [knowledge template](../../templates/guidance-template.md).

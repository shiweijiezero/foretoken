# Qwen 解码速度优化

[English](qwen-decode.md) | 简体中文

## 目标

提高 Qwen3.5-35B-A3B BF16 在单请求负载下的解码速度，同时保持回答质量。主要比较每输出 token 耗时（TPOT）和输出 token/s。

## 环境

两台机器，每台配备 8 张 MetaX C500 GPU。使用 Qwen3.5-35B-A3B BF16，GPU 数量和并行方式由 Agent 根据测量选择。

## 优化范围

不限定优化组件的范围。

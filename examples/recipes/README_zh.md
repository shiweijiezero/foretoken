<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型部署配方

[English](README.md) | 简体中文

面向具体模型的 Foretoken 部署配置。每份配方包含硬件和运行时要求、配置文件，以及请求和清理命令。

| 模型 | 硬件与精度 |
|---|---|
| [GLM-5.3-Flash](glm-5.3-flash/metax-bf16/) | 双机 16 张沐曦 C500、BF16、MTP、共享内存 KV |
| [MiniMax H3](minimax-h3/a100-bf16-tp2/) | 单机 2 张 NVIDIA A100 80 GB、BF16、TP=2；根据图片或参考视频生成 |

<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Model recipes

English | [简体中文](README_zh.md)

Model-specific Foretoken deployments. Each recipe includes its hardware and runtime requirements, configuration, request and cleanup commands.

| Model | Hardware and precision |
|---|---|
| [GLM-5.3-Flash](glm-5.3-flash/metax-bf16/) | Two nodes, 16 MetaX C500 GPUs, BF16, MTP and shared memory KV |
| [MiniMax H3](minimax-h3/a100-bf16-tp2/) | One node, 2 NVIDIA A100 80 GB GPUs, BF16, TP=2; image- or video-conditioned generation |

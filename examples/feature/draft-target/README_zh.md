<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Draft/Target 分离

[English](README.md) | 简体中文

将 `Qwen/Qwen3-8B` 与 EAGLE3 草稿模型部署到不同 GPU。Draft 提出候选 token，Target 验证后通过普通生成接口返回结果。多个 Target 副本可以共用一个 Draft。

示例使用[从源码安装的平台](../../../docs/deployment/source_zh.md)及其 vLLM 0.31 运行环境，需要两张 GPU，每个模型实例一张。连同前端共申请 10 核 CPU 和 98 GiB 主机内存。两个模型均为公开模型，模型文件使用平台默认存储；该配置用于不带 LoRA 的文本生成。

## 部署并发送请求

在仓库根目录执行：

```bash
foretoken deploy examples/feature/draft-target --timeout 20m
FRONTEND_URL="$(foretoken endpoint examples/feature/draft-target)"
REQUEST_HOST="$(foretoken endpoint examples/feature/draft-target --host)"

curl --fail-with-body --no-buffer "$FRONTEND_URL/v1/chat/completions" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"Explain how speculative decoding works."}],"max_tokens":256,"stream":true}'
```

调用方只访问 Target 模型。Draft Pool 的 `model` 选择草稿权重，Target 的 `speculation.draftPools` 引用同一模型服务内 Draft Pool 的 `name`。算法参数统一放在 Draft 的原生 `speculative-config` 中。

## 共享 Draft

将 `model.yaml` 中 `target` Pool 的 `replicas` 改为 `2` 或 `3`，再次执行部署命令，即可分别运行三卡或四卡配置。每个 Draft、Target 实例使用一张 GPU；增加副本不改变引擎的张量并行度。

优化共享 Draft 的性能时，在 Draft Pool 的 `engineArgs` 中使用 `distributed-executor-backend: uni`。其他已支持的单 Worker 执行器沿用普通草稿调度路径。Draft 的原生批次上限用于所有 Target 的合计负载；超过单批容量的请求会分批执行。

各 Pool 可以通过自己的 `engineArgs.async-scheduling` 开启或关闭异步调度。做同步对照时，将两端都设为 `false`。EAGLE3 需要完整提示词对应的特征，因此该配置会自动关闭前缀缓存。

性能测量通过[评测的数据集入口](../../../benchmarks/docs/perf/huggingface_zh.md)读取 [ShareGPT 原始对话](https://huggingface.co/datasets/anon8231489123/ShareGPT_Vicuna_unfiltered)，GPU 数包含 Draft 与 Target。与共置推理比较时，保持模型、候选 token 数、调度模式、前缀缓存设置和负载一致。

## 清理

```bash
foretoken delete examples/feature/draft-target
```

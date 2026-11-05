<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Draft–Target 分置

[English](README.md) | 简体中文

让 vLLM 的 Target 模型和 EAGLE Draft 模型运行在不同 GPU 上。Target 提供
OpenAI 兼容 API，沿用原生调度、验证和采样；Draft 使用自己的 KV 缓存生成候选。
这是实验性适配器，通过 `--worker-cls` 和 `--additional-config` 显式启用。

## 启动本地双进程

需要 Linux、两张 CUDA GPU，以及从提交
`1be36283678a9a94fc8fdaad6c95c2896d6b4015` 构建的 vLLM。这是
[`data-plane/Cargo.toml`](../../../Cargo.toml) 固定的源码版本。准备环境时，按
[vLLM GPU 安装指南](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/#build-wheel-from-source)
构建该提交。两个进程都需要能读取 Target 配置和 Draft 模型权重。适配器使用
vLLM 已有的 PyTorch、NumPy、PyZMQ 和 NCCL 依赖。

下面的独立启动方式已在同机两张 A100 上验证，模型为 `Qwen/Qwen3-8B` 和
`Tengyunw/qwen3_8b_eagle3`，每轮生成三个候选。适配器尚未接入 Foretoken 的
Kubernetes 部署。默认 model-server 镜像使用另一个 vLLM 版本；运行下面的命令
需要上述固定版本环境。

在仓库根目录打开终端，启动 Draft：

```sh
CUDA_VISIBLE_DEVICES=1 \
PYTHONPATH="$PWD/data-plane/model-server/python${PYTHONPATH:+:$PYTHONPATH}" \
python -m foretoken_dt --listen tcp://127.0.0.1:5557
```

Draft 会等待 Target 发来配置及共享词表权重。在另一个终端中，同样从仓库根目录
启动 Target：

```sh
CUDA_VISIBLE_DEVICES=0 VLLM_USE_V2_MODEL_RUNNER=1 \
PYTHONPATH="$PWD/data-plane/model-server/python${PYTHONPATH:+:$PYTHONPATH}" \
vllm serve Qwen/Qwen3-8B \
  --worker-cls foretoken_dt.target.TargetWorker \
  --speculative-config '{"model":"Tengyunw/qwen3_8b_eagle3","method":"eagle3","num_speculative_tokens":3}' \
  --additional-config '{"foretoken_dt":{"endpoint":"tcp://127.0.0.1:5557","early_feature_send":true}}' \
  --dtype bfloat16 --no-async-scheduling --no-enable-prefix-caching \
  --max-model-len 1024 --max-num-seqs 16 --max-num-batched-tokens 128
```

等待日志出现 `Draft model and KV cache ready`，且 Target 的 API 启动完成后，
在第三个终端中发送请求：

```sh
curl --fail-with-body --no-buffer http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"Explain speculative decoding briefly."}],"temperature":0,"max_tokens":128,"stream":true}'
```

Target 会流式返回生成结果。使用 Ctrl-C 停止两个进程。开启新会话或候选生成失败
后，都需要重新启动两个进程。

## 配置与限制

一个 Draft 进程服务一个 Target 进程，每个进程使用一张 GPU。支持 EAGLE 和
EAGLE3、未量化的文本模型、固定候选数量及 greedy Draft 采样。Target 可以使用
greedy 或随机采样，也支持结构化输出。支持分块 prefill 和多请求 batch。

必须使用同步调度，并关闭前缀缓存。两端的 KV 缓存 dtype 必须为 `auto`，最大
上下文长度保持一致。不支持张量、流水线、数据及上下文并行、多模态输入、LoRA、
KV 传输、并行 Draft、动态候选数量、在线更新权重和 sleep mode。

在 `--additional-config` 的 `foretoken_dt` 对象中设置适配器选项：

| 选项 | 默认值 | 用途 |
| --- | --- | --- |
| `endpoint` | 必填 | Draft 的 ZeroMQ 控制端点，与 `--listen` 对应。 |
| `transport` | `"nccl"` | 用 NCCL 传输 CUDA 张量；`"tcp"` 会经 CPU 内存中转。 |
| `early_feature_send` | `false` | 使用 EAGLE3 和 NCCL 时，让满足条件的 Draft 输入准备、特征传输与 Target 计算重叠。示例已开启。 |
| `timeout_seconds` | `300` | 每次 socket 等待或 CUDA 传输完成的超时时间，单位为秒。首次加载模型较慢时可调大。 |

使用 TCP 传输或原始 EAGLE 方法时，`early_feature_send` 必须为 `false`。
NCCL 根据拓扑和运行配置选择实际链路。本适配器尚未验证跨机 RDMA。
Draft 端点没有身份认证，应置于可信私有网络中。

Draft 会测量激活内存用量，再根据可用显存分配 KV 缓存。如需指定大小，在 Draft
命令中添加 `--kv-cache-memory-bytes BYTES`。CUDA graph capture 也会占用显存；
若捕获时显存不足，可缩小 KV 缓存或 graph capture 的规模。

检测到超时或候选生成失败后，会结束会话，不自动重试。传输超时无法中断阻塞中的
底层 NCCL 初始化或提交调用；若进程停在这些调用中，需要停止两个进程。

## 与共置比较

停止两个进程，再从 Target 命令中去掉 `--worker-cls` 和 `--additional-config`，
即可使用原生 EAGLE 共置。保持模型对、dtype、候选数量、batch 限制和工作负载
一致，按[基准测试流程](../../../../benchmarks/README_zh.md)比较完整输出、延迟和吞吐。
性能测量与 profiling 分开运行。

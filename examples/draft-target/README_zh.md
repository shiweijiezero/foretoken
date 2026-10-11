<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Draft/Target 部署

[English](README.md)

为 `Qwen/Qwen3-4B` 配置独立的 `Qwen/Qwen3-0.6B` Draft，通过兼容 OpenAI 的 API 调用主模型。
两个模型分别运行在 Aggregate Pool 中，可以独立调整副本数。

这个 Kubernetes 示例使用独立的自回归 Draft。DFlash、EAGLE/EAGLE3、DSpark 和 MTP
请使用[独立 neural Draft 指南](../../data-plane/dt-plugin/README_zh.md#在-nvidia-上运行神经-draft)。
当前 ModelService 部署路径尚未开放 neural `speculative-config` 配置。

## 部署并发送请求

需要具有 GPU 支持的 Kubernetes 集群、至少两张可用 GPU 和共享模型存储。
默认资源申请为 Draft 4 CPU、16 GiB 内存，Target 4 CPU、32 GiB 内存，
另加 frontend 2 CPU、2 GiB 内存。示例的 [RuntimeCache](cache.yaml) 使用仓库中的
`data/` 目录；集群共享存储的准备方式见[模型存储](../../docs/model-storage_zh.md)。

DT 需要从源码构建的平台及其支持的引擎。NVIDIA 构建会自动准备 CUDA 13 运行环境和 DT 插件；
主机驱动需兼容 CUDA 13，默认 batch-invariant 模式要求 GPU 计算能力至少为 8.0。
沐曦或自定义引擎镜像的要求见[角色兼容条件](../../data-plane/dt-plugin/README_zh.md#启动两个角色)。
构建工具要求见[源码安装](../../docs/custom-deployment_zh.md)。

在仓库根目录安装源码 CLI 和平台，然后部署：

```bash
python -m pip install -e .
foretoken install -e .
foretoken deploy examples/draft-target --timeout 20m
export FRONTEND_URL="$(foretoken endpoint examples/draft-target)"

curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-4B","messages":[{"role":"user","content":"Explain speculative decoding in two sentences."}],"temperature":0,"max_tokens":128,"chat_template_kwargs":{"enable_thinking":false}}'
```

如果已安装当前 checkout 对应的源码平台，从 `foretoken deploy` 开始即可。
返回的 `choices[0].message.content` 是 Target 确认的回答，`usage` 包含 token 用量。
`enable_thinking: false` 让这个 Qwen3 示例直接回答；需要流式输出时，在 JSON 中添加
`"stream": true`，并给 curl 添加 `--no-buffer`。最小部署不需要 RDMA，使用 `temperature: 0`。

## 使用 Python 调用

运行 `python -m pip install openai` 安装 SDK，然后使用上面导出的 `FRONTEND_URL`：

```python
import os
from openai import OpenAI

with OpenAI(
    base_url=os.environ["FRONTEND_URL"].rstrip("/") + "/v1",
    api_key="unused",
) as client:
    response = client.chat.completions.create(
        model="Qwen/Qwen3-4B",
        messages=[{"role": "user", "content": "Explain speculative decoding in two sentences."}],
        temperature=0,
        max_tokens=128,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    print(response.choices[0].message.content)
    print(response.usage)
```

`api_key` 用于满足 SDK 接口要求；此示例直连 frontend，没有认证网关。
应用只需提供 Target 模型 ID 和普通 messages，不必管理 Draft session 或 verification round。

## 选择模型和副本数

编辑 [model.yaml](model.yaml)：

| 配置 | 含义 |
| --- | --- |
| `spec.model` | 对外服务的 Target 模型；修改后也要更新客户端 model ID。 |
| `spec.speculation.draftPool` | Draft Pool 的名称，本例为 `draft`。 |
| Draft Pool 的 `model` | Draft 权重；只有被引用的 Draft Pool 可以覆盖服务模型。 |
| 各 Pool 的 `replicas` | 独立的实例数，每个实例使用一张 GPU。 |
| `spec.engineArgs.max-model-len` | 同时应用于两端的上下文上限，prompt 加输出长度不能超过它。 |

两边都保留 `role: aggregate`，继承服务的模型来源和 tokenizer，默认使用 Target tokenizer。
需要选择 token ID 含义兼容的模型。使用 `source: local` 时，模型和 tokenizer
路径必须是 Pod 内可见的绝对路径。

运行两个 Draft 和两个 Target 时，把两个 Pool 的 `replicas` 都改为 `2`，再执行
`foretoken deploy examples/draft-target --timeout 20m`。这需要四张 GPU，以及两倍的
模型 Pool CPU/内存；frontend 配置和客户端 API 不变。只增加 Draft 容量时，
仅修改 Draft Pool 的副本数。多个 Draft 副本提供容量，不会共同生成候选树。

需要与 Target-only 对比时，从 `model.yaml` 删除 `spec.speculation` 和 Draft Pool，
再重新部署；保持 Target 模型和客户端采样设置一致。

frontend 每轮调度 ready request，可以在下一轮选择其他 Draft；已有 Target session
继续使用原实例。缩容会等待已有 session 结束，没有空闲 GPU 时滚动更新可能导致服务短暂不可用。
取消、服务更新和自定义 scheduling 的细节见[前端执行说明](../../data-plane/dt-plugin/docs/frontend-workflow.md)。

## 启用随机采样

在具备 RDMA 的集群中，`foretoken install` 会发现已有的共享 RDMA 资源，或准备共享 device plugin。
需要选择外部资源分配时，才在平台 values 中覆盖 `rdma.resourceName`、`rdma.resourceCount`。
有可用资源后，控制器为两端分配 RDMA 设备并启用 Mooncake 传输。Worker 使用 Pod IP；
网络需允许角色 HTTP 和 Mooncake 动态握手端口。运行环境还需具备匹配的 Mooncake
构建和 GPU 内存注册支持；资源申请成功不等于已经确认实际传输路径。

启用后，请求可使用 `temperature`、`top_p`、`top_k` 和 Target `seed`。
Draft 的随机数独立于该 seed。未启用 RDMA 时，随机采样请求会被拒绝。

## 支持范围与后续实验

当前支持文本输入和线性候选，每个角色使用一个 GPU Worker、eager 执行和本地同步调度。
尚不支持 P/D 组合、多模态、KV offload、结构化输出、非默认 penalties、`min_p` 或 profiling。
模型 generation 配置中的非默认 penalties 同样会导致拒绝，见[采样要求](../../data-plane/dt-plugin/README_zh.md#启动两个角色)。

自定义 scheduling 使用 [batch 示例](../../data-plane/dt-plugin/docs/frontend-workflow.md#run-per-round-batches-through-router)，
调用者选择目标实例的 Target KV migration 使用 [migration API](../../data-plane/dt-plugin/docs/frontend-workflow.md#move-a-request-between-targets)。
公开请求不会自动迁移 Target。Kubernetes RDMA 网络、依赖指标的自动扩缩容和性能收益尚未验证。

## 删除部署

```bash
foretoken delete examples/draft-target
```

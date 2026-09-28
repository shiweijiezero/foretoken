<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Draft/Target 部署

[English](README.md)

本示例将主模型和独立 Draft 模型分别部署为 Aggregate Pool，由现有前端 Router 自动发现和选择实例。
需要两张 GPU、共享 RuntimeCache，以及包含[独立 external-speculation 引擎扩展](../../data-plane/dt-plugin/docs/mrv2-integration.md)的源码构建平台。
已发布和仓库固定版本的 vLLM 不包含该扩展。安装平台时，把 `runtime.vllm.image`
设为包含该扩展的 model-server 镜像；只安装 Python DT 包不会增加引擎接口。
通过 RDMA 传提议分布时，两端都需要扩展引擎。

平台配置现有 `rdma.resourceName`、`rdma.resourceCount` 后，控制器会为每个角色分配
RDMA 设备并启用 Mooncake GPU 概率传输。Worker 使用 Pod IP 建立握手；网络需允许
HTTP 和 Mooncake 动态握手端口。控制器允许同一服务的 D/T Pod 互通这些端口。
Mooncake 从可见 HCA 中选择，资源申请本身不能证明 NIC 隔离。镜像还需具备匹配的
Mooncake wheel 和 GPU 内存注册支持。

没有 RDMA 分配时，此示例只接受温度 0。启用 RDMA 后，请求可使用 `temperature`、
`top_p`、`top_k` 和 Target 的 `seed`。

角色启动器默认在两端启用 vLLM batch invariance。使用计算能力 8.0 及以上的 NVIDIA GPU
和兼容的引擎后端；原生环境变量覆盖方式及数值、性能限制见
[角色启动要求](../../data-plane/dt-plugin/README_zh.md#启动两个角色)。

安装好上述平台后，在仓库根目录执行：

```bash
foretoken deploy examples/draft-target --timeout 20m
FRONTEND_URL="$(foretoken endpoint examples/draft-target)"
curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-4B","messages":[{"role":"user","content":"Hello"}],"temperature":0,"max_tokens":256}'
```

`spec.model` 指定主模型，`spec.speculation.draftPool` 引用负责产生候选的 Pool。
该 Pool 必须通过 `modelPools[].model` 指定 Draft 权重；其余 Pool 使用主模型并负责验证。
两边都保留 `role: aggregate`。普通 Aggregate 部署不配置 `speculation`，也不配置独立 Draft Pool。
只有被引用的 Draft Pool 可以覆盖模型。两者继承服务的模型来源和 tokenizer，默认使用主模型的
tokenizer。需要选择 token ID 含义兼容的模型，不能只根据名称判断。
当前 `source: local` 部署要求模型、tokenizer 使用 Pod 内可见的绝对路径。

分别修改各 Pool 的 `replicas` 后重新部署，即可独立调整副本数。新请求各选择一个
Draft 和 Target。缩容先关闭准入，再撤销路由，等待已有会话结束；model-server 还会在关闭时限内
等待持有的传输产物释放。
多个 Draft 副本提供更多容量，当前不会共同生成候选树。

当前限制：文本输入、线性候选、每个角色实例一张 GPU、eager 执行、本地同步调度。
不支持 P/D 组合、多模态、KV offload/传输、结构化输出、profiling 或 `min_p`。
HTTP 传候选 ID 和描述符；完整提议分布由 Mooncake 在 GPU Worker 间传输。
Draft 随机数独立于 Target seed。Kubernetes RDMA 网络、依赖指标的自动扩缩容、
随机采样质量评估和公开 API 性能基准尚未验证。没有空闲 GPU 容量时，
滚动更新可能导致服务短暂不可用。

```bash
foretoken delete examples/draft-target
```

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 实验性 Draft/Target 部署

[English](README.md)

本示例分别部署 Draft、Target Pool，由现有前端 Router 自动发现和选择实例。
需要两张 GPU、共享 RuntimeCache，以及包含[独立 external-speculation 引擎扩展](../../data-plane/dt-plugin/docs/mrv2-integration.md)的源码构建平台。
已发布和仓库固定版本的 vLLM 不包含该扩展。安装平台时，把 `runtime.vllm.image`
设为包含该扩展的 model-server 镜像；只安装 Python DT 包不会增加引擎接口。

安装好上述平台后，在仓库根目录执行：

```bash
foretoken deploy examples/draft-target --timeout 20m
FRONTEND_URL="$(foretoken endpoint examples/draft-target)"
curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-4B","messages":[{"role":"user","content":"Hello"}],"temperature":0,"max_tokens":256}'
```

`spec.model` 指定 Target。Draft 必须通过 `modelPools[].model` 指定自己的模型，
其他角色不能覆盖该字段。两者继承服务的模型来源和 tokenizer，默认使用 Target 的
tokenizer。需要选择 token ID 含义兼容的模型，不能只根据名称判断。
当前 `source: local` 部署要求模型、tokenizer 使用 Pod 内可见的绝对路径。

分别修改各 Pool 的 `replicas` 后重新部署，即可独立调整副本数。新请求各选择一个
Draft 和 Target。缩容先关闭准入，再撤销路由，并在 drain 时限内等待已有会话结束。
多个 Draft 副本提供更多容量，当前不会共同生成候选树。

当前限制：文本输入、贪心采样、每个角色实例一张 GPU；不支持与 P/D 组合、多模态、
KV offload/传输、结构化输出或 profiling。DT 角色尚未提供 scheduler 指标，
尚未验证依赖性能指标的自动扩缩容策略。候选 token 通过 HTTP 传递；独立 Mooncake
诊断不在这条推理链路中。

```bash
foretoken delete examples/draft-target
```

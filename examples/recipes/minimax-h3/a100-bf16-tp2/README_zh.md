<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 双 NVIDIA A100 上的 MiniMax H3 BF16

[English](README.md) | 简体中文

使用 MiniMax H3 的 FL2VA 模式根据参考图片生成视频，或使用 Ref2VA 模式根据参考视频生成。
本配方部署一个前端服务和一个 BF16 模型副本，将模型以张量并行方式分布到两张 GPU 上（TP=2）。
模型申请两张 A100 80 GB、32 个 CPU 核和 256 GiB 主机内存；还需为平台预留资源。

## 构建和安装

准备可用的 GPU Kubernetes 集群，以及[源码部署指南](../../../../docs/custom-deployment_zh.md)
列出的工具。本地集群可按 [k3d 指南](../../../../docs/k3d-deployment_zh.md)搭建。
在 Foretoken 仓库根目录执行：

```bash
make image-vllm-omni VLLM_OMNI_IMAGE=foretoken-vllm-omni:latest
make image-model-server-omni \
  INFERENCE_ENGINE_IMAGE=foretoken-vllm-omni:latest \
  OMNI_MODEL_SERVER_IMAGE=foretoken-omni-model-server:latest
```

使用 k3d 时，将模型服务镜像导入 `CLUSTER` 指定的集群：

```bash
k3d image import --cluster "$CLUSTER" foretoken-omni-model-server:latest
```

远程集群需要将该镜像打标签并推送到节点可访问的镜像仓库，再将下方的镜像地址替换为推送后的地址。
把以下配置保存为 `platform-values.yaml`：

```yaml
runtime:
  vllmOmni:
    image: foretoken-omni-model-server:latest
```

安装或更新平台。远程集群还需按源码部署指南添加 `--registry`。

```bash
foretoken install -e . --values platform-values.yaml
```

## 部署

默认从公开 Hugging Face 仓库 `MiniMaxAI/MiniMax-H3` 自动下载权重，
与 Quick Start 示例共用仓库根目录的 `data/`。本地 k3d 复用建集群时的数据挂载；
远程集群按[模型存储指南](../../../../docs/model-storage_zh.md)，将 `cache.yaml`
中的 `directory` 改为目标节点可访问的目录。

使用 Gateway 时，先在本配方的 `frontend.yaml` 中设置 `spec.hostname`，
并按[网关模式](../../../../README_zh.md#网关模式)安装平台，再部署模型。
下方请求命令同时适用于 LoadBalancer 和 Gateway。

```bash
RECIPE=examples/recipes/minimax-h3/a100-bf16-tp2
foretoken deploy "$RECIPE" --timeout 1h
```

## 根据图片生成视频

将 `REFERENCE_IMAGE` 设置为已有 PNG 文件的路径：

```bash
REFERENCE_IMAGE=/path/to/reference.png
ENDPOINT="$(foretoken endpoint "$RECIPE" --timeout 10m)"
REQUEST_HOST="$(foretoken endpoint "$RECIPE" --host --timeout 10m)"
curl --fail-with-body --max-time 4000 \
  "${ENDPOINT%/}/v1/videos/sync" \
  -H "Host: $REQUEST_HOST" \
  -F model=MiniMaxAI/MiniMax-H3 \
  -F 'prompt=A cinematic tracking shot of a sailboat crossing a calm bay at sunrise.' \
  -F "input_reference=@${REFERENCE_IMAGE};type=image/png" \
  -F width=1024 -F height=576 -F num_frames=124 -F fps=24 \
  -F num_inference_steps=50 -F aspect_ratio=16:9 -F flow_shift=12 -F seed=1 \
  -F 'extra_params={"task":"fl2va","audio_flow_shift":3}' \
  --output h3-fl2va.mp4
```

生成的视频保存到当前目录的 `h3-fl2va.mp4`。

## 根据参考视频生成

将 `model.yaml` 中的 `spec.engineArgs.task-type` 改为 `ref2va`。
等待正在生成的请求结束后，先删除部署以释放两张 GPU，再部署另一种模式。
将 `REFERENCE_VIDEO` 设置为已有 MP4 文件的路径：

```bash
REFERENCE_VIDEO=/path/to/reference.mp4
foretoken delete "$RECIPE" --timeout 2h
foretoken deploy "$RECIPE" --timeout 1h
ENDPOINT="$(foretoken endpoint "$RECIPE" --timeout 10m)"
REQUEST_HOST="$(foretoken endpoint "$RECIPE" --host --timeout 10m)"
curl --fail-with-body --max-time 4000 \
  "${ENDPOINT%/}/v1/videos/sync" \
  -H "Host: $REQUEST_HOST" \
  -F model=MiniMaxAI/MiniMax-H3 \
  -F 'prompt=Continue the scene shown in the reference video with a smooth camera movement.' \
  -F "input_references=@${REFERENCE_VIDEO};type=video/mp4" \
  -F width=1024 -F height=576 -F num_frames=124 -F fps=24 \
  -F num_inference_steps=50 -F aspect_ratio=16:9 -F flow_shift=12 -F seed=1 \
  -F 'extra_params={"task":"ref2va","audio_flow_shift":3}' \
  --output h3-ref2va.mp4
```

生成的视频保存为 `h3-ref2va.mp4`。切回图片输入时，将 `task-type` 改回 `fl2va`，
重新执行删除和部署命令，再发送 FL2VA 请求。

## 其他模型来源

使用 ModelScope 时，修改 `model.yaml` 中的以下字段，并将请求中的 `model`
改为 `MiniMax/MiniMax-H3`：

```yaml
spec:
  model: MiniMax/MiniMax-H3
  source: modelscope
```

使用 Hugging Face 兼容镜像站时，将地址加入 `platform-values.yaml`，
在部署模型前重新执行平台安装命令：

```yaml
runtime:
  vllm:
    modelSource:
      endpoint: https://your-huggingface-compatible-mirror.example
```

保留同一文件中的 `runtime.vllmOmni.image`。此地址用于 Hugging Face 下载。
使用离线权重时，按[模型来源指南](../../../../docs/model-sources_zh.md)选择 `source: local`，
并将模型标识设为模型根目录或所选 FL2VA/Ref2VA 目录；请求中使用相同的模型标识。

## 清理

```bash
foretoken delete "$RECIPE" --timeout 2h
```

共享数据目录会保留，供后续部署复用。

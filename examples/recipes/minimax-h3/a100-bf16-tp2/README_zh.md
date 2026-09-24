<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# MiniMax H3 FL2VA 双卡 A100 原生 BF16

[English](README.md) | 简体中文

在单节点的两张 NVIDIA A100 80 GB GPU 上运行一个原生 BF16 的 MiniMax H3
FL2VA 模型组。Foretoken 通过薄适配器 `foretoken-omni-model-server` 管理
vLLM-Omni 进程的生命周期，并直接暴露生成的 ModelGroup Service。

运行时镜像、Kubernetes 部署、H3 权重加载，以及一次完整的 FL2VA 视频请求，
已在两张 NVIDIA A100 80 GB GPU 上验证。但该次使用的本地 vLLM-Omni 镜像
尚不能从已发布的源码版本重建；所缺的源码输入见下文。

## 准备

### 构建 H3 vLLM-Omni 基础镜像

基础镜像需要包含兼容 H3 的 vLLM-Omni Python 包及其插件。已验证的本地镜像
基于上游提交 `a4ea67a21b20054dacc6e83952f9bd407e8ee4e7` 的源码，
但还包含未发布为提交或补丁集的本地修改。**只检出该上游提交，不能重建出
已经验证的双卡镜像。** 在其他环境构建前，需先把必要的 H3 修改整理为固定、
可审查的源码版本，并检出该版本；不能依赖开发者的 Conda 环境或未提交工作树。

在干净的 H3 兼容 vLLM-Omni 源码目录根部构建 CUDA 镜像。Omni 的 Dockerfile
会将该源码安装到以 vLLM 0.26.0 为基础的镜像中，不会复制宿主机 Python 环境。
同时记录源码版本和镜像 ID：

```bash
git status --short
test -z "$(git status --porcelain)"
git rev-parse HEAD
DOCKER_BUILDKIT=1 docker build \
  --build-arg BASE_IMAGE=vllm/vllm-openai:v0.26.0 \
  -f docker/Dockerfile.cuda \
  -t vllm-omni-h3:bf16-tp2 .
docker run --rm --entrypoint python vllm-omni-h3:bf16-tp2 \
  -m pip show vllm vllm-omni
docker image inspect vllm-omni-h3:bf16-tp2 --format '{{.Id}}'
```

若需精确重建，还应固定上游基础镜像 digest。Omni 的 Dockerfile 在构建时解析
Python 依赖；需要日后得到同一组依赖时，还须记录或锁定依赖版本。固定的 H3
源码版本是本配方目前尚未提供的前提，不能将上述命令视为已完成独立复现。

### 叠加 Foretoken 适配器

回到 Foretoken 仓库根目录，在 H3 Omni 镜像上构建运行时镜像。这一步只加入
Foretoken 适配器，不安装 H3，也不打包模型权重。使用本地 k3d 集群时，先按
[k3d 指南](../../../../docs/k3d-deployment_zh.md)设置 `CLUSTER`，再导入镜像：

```bash
make image-model-server-omni \
  INFERENCE_ENGINE_IMAGE=vllm-omni-h3:bf16-tp2 \
  OMNI_MODEL_SERVER_IMAGE=foretoken-omni-model-server:h3
k3d image import --cluster "$CLUSTER" foretoken-omni-model-server:h3
```

非 k3d 集群则需将镜像标记并推送到节点可访问的仓库，并将下方本地镜像名
替换为推送后的地址。在 Foretoken 平台 values 中指定镜像：

```yaml
runtime:
  vllmOmni:
    image: foretoken-omni-model-server:h3
```

将上述 values 保存为 `platform-values.yaml`，再安装或更新 Foretoken：

```bash
foretoken install -e . --values platform-values.yaml
```

### 准备 FL2VA 权重

MiniMax H3 模型仓库需要授权访问，构建机还需安装 `hf` 命令行工具。权重与
镜像分开下载；下面固定的是已验证权重的版本。选择目标 GPU 节点已挂载的
绝对数据目录；
[k3d 指南](../../../../docs/k3d-deployment_zh.md)在创建集群时挂载仓库的
`data` 目录：

```bash
mkdir -p data
DATA_ROOT="$(realpath data)"
hf auth login
hf download MiniMaxAI/MiniMax-H3 \
  --revision 42ed227ee7df40d41602854ae760620d6eb651fe \
  --include 'model_index.json' 'FL2VA/*' \
  --local-dir "$DATA_ROOT/models/MiniMax-H3"
test -f "$DATA_ROOT/models/MiniMax-H3/FL2VA/model_index.json"
```

将本配方 `cache.yaml` 中的 `spec.directory` 改成 `DATA_ROOT` 的绝对路径，
不要填 `models/MiniMax-H3/FL2VA` 子目录。部署时权重应位于：

```text
<data-root>/models/MiniMax-H3/FL2VA/
```

如果 k3d 集群已经创建，部署前需确认节点确实挂载了相同数据目录。权重不会
复制进两个 Docker 镜像；不要将 Hugging Face 凭据放入模型目录或镜像构建上下文。

目录存储和动态存储的说明见[模型存储](../../../../docs/model-storage_zh.md)。
清单默认申请两张 GPU、32 个 CPU 核和 256 GiB 主机内存；可根据目标节点调整
CPU 和内存。

## 部署与请求

在仓库根目录执行：

```bash
RECIPE=examples/recipes/minimax-h3/a100-bf16-tp2
foretoken deploy "$RECIPE" --timeout 1h
kubectl -n foretoken-h3 get modelservices,modelpools,modelgroups,pods -w
```

控制器根据申请的 GPU 拓扑自动生成 `num-gpus=2`、
`tensor-parallel-size=2`、`usp=1` 和 `ring=1`。本配方不创建
`FrontendService`，因为当前 token frontend 不负责视频生成请求的路由。
请直接转发生成的 ModelGroup Service：

```bash
SERVICE="$(
  kubectl -n foretoken-h3 get service \
    -l inference.foretoken.io/model-group \
    -o jsonpath='{.items[0].metadata.name}'
)"
kubectl -n foretoken-h3 port-forward "service/$SERVICE" 8091:9000
```

在另一个终端发送同步视频请求：

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:8091/v1/videos/sync \
  -F 'prompt=A cinematic tracking shot of a sailboat crossing a calm bay at sunrise.' \
  -F width=1024 \
  -F height=576 \
  -F num_frames=124 \
  -F fps=24 \
  -F num_inference_steps=50 \
  -F aspect_ratio=16:9 \
  -F flow_shift=12 \
  -F seed=1 \
  -F 'extra_params={"task":"fl2va","audio_flow_shift":3}' \
  --output h3-fl2va.mp4
```

Omni 镜像将同步视频请求超时设为 4000 秒；构建镜像时可通过
`OMNI_VIDEO_SYNC_TIMEOUT` 覆盖。独立的 `timeouts.drain` 控制 Pod 关闭时
已接收请求的排空时间。

清理工作负载：

```bash
foretoken delete "$RECIPE" --timeout 10m
```

已配置数据目录中的权重和运行时缓存文件会保留。

<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 分离图片编码、预填充与解码

[English](README.md) | 简体中文

将 `Qwen/Qwen2.5-VL-3B-Instruct` 的编码（Encoder）、预填充（Prefill）和解码（Decode）分别部署为三个 Pool。示例共申请四张 GPU：编码和预填充各一张，解码使用两张卡做张量并行。客户端仍通过普通的 Chat Completion 接口发送请求。

## 平台准备

本示例使用源码安装，需要四张 NVIDIA GPU。从仓库根目录构建所用的推理引擎基底镜像：

```bash
docker build -f examples/encoder-prefill-decode/runtime.Dockerfile -t foretoken-vllm:epd .
```

集群需要支持 `ReadWriteMany` 的 StorageClass。默认 StorageClass 不支持共享存储时，在 `encoder-cache.yaml` 中填写适用的 `storageClassName`。RDMA 传输还需要启用 GPUDirect RDMA 并分配 RDMA 设备；不具备该条件时，可按下文选择 TCP。

将镜像选择和共享编码缓存设置保存为 `platform-values.yaml`：

```yaml
runtime:
  vllm:
    image: foretoken-vllm:epd
    ec:
      sharedStorageClaim: encoder-cache
```

P/D 默认使用 RDMA。如需选择 Mooncake TCP，将以下设置合并到同一份 `runtime.vllm` 配置中：

```yaml
runtime:
  vllm:
    pd:
      protocol: tcp
```

TCP 不需要 RDMA 设备，GPU KV 数据会经过主机内存暂存。

从当前源码安装平台：

```bash
foretoken install -e . --values platform-values.yaml
```

远程集群还需通过 `--registry REGISTRY` 指定节点能够访问的容器仓库。其他安装选项见 [CLI 指南](../../cli/README_zh.md#当前源码)。

## 部署

从仓库根目录执行：

```bash
foretoken deploy examples/encoder-prefill-decode --timeout 20m
export FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/encoder-prefill-decode)"
```

## 发送图片请求

将一张 JPEG 保存为 `image.jpg`，编码为 base64 data URL 后发送：

```bash
python - <<'PY'
import base64
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

image = base64.b64encode(Path("image.jpg").read_bytes()).decode()
body = {
    "model": "Qwen/Qwen2.5-VL-3B-Instruct",
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "请描述这张图片。"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
    ]}],
    "max_tokens": 128,
}
request = Request(
    os.environ["FORETOKEN_FRONTEND_URL"] + "/v1/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
)
with urlopen(request, timeout=300) as response:
    print(json.load(response)["choices"][0]["message"]["content"])
PY
```

## 调整容量

每个阶段各有一个 Pool，其 `replicas`、资源请求和 `engineArgs` 可以独立调整。Pool 的 `engineArgs` 会整体替换服务级字典，示例通过 YAML anchor 复用共同设置。GPU 请求数量与并行参数的对应关系见[推理参数](../../docs/inference-parameters_zh.md)。

修改后重新运行 `foretoken deploy`，通过 `foretoken status examples/encoder-prefill-decode` 查看就绪状态。编码结果经共享卷传递，Prefill 的 KV Cache 通过平台选择的 Mooncake 传输交给 Decode；客户端不需要填写传输地址或传递中间结果。

## 清理

```bash
foretoken delete examples/encoder-prefill-decode
```

命令会删除示例命名空间和缓存卷声明。编码文件是跨请求复用的缓存，不在单次请求结束时删除；底层存储是否保留取决于卷的回收策略。共享 Foretoken 平台继续保留。

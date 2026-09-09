<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 持久化运行时缓存

[English](runtime-cache.md) | 简体中文

## 复用一个数据目录

当前源码的示例将下载的模型、分词器文件和引擎缓存集中存放在 `./data`。目录模式需要使用同一源码构建的 CLI 和控制器，已发布的 0.0.2 包不提供该能力。

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: RuntimeCache
metadata:
  name: models
spec:
  directory: ./data
  accessMode: ReadWriteMany
```

`directory` 与 `initialSize`、`maxSize`、`storageClassName` 互斥。目录模式不设置容量上限，实际空间和配额由底层文件系统决定。静态 PV 与 PVC 中的内部申请值只是为了满足 Kubernetes 的绑定要求，不表示 Foretoken 预分配或限制了相应磁盘空间。

### 本机 k3d

创建集群前准备示例目录，并让工作负载用户可以读写它（标准镜像使用 UID/GID 65532）。将目录映射到计划运行 frontend 和模型的节点：

```bash
mkdir -p examples/quickstart/data
k3d cluster create "$CLUSTER" \
  --config deploy/k3d/config.yaml \
  --volume "$PWD/examples/quickstart/data:/var/lib/foretoken/data@all"
```

创建 GPU 节点时还需要保留 [k3d 指南](../k3d-deployment_zh.md)中的 GPU 配置。随后安装当前源码并部署：

```bash
pip install -e .
foretoken install -e .
foretoken deploy examples/quickstart
```

CLI 相对 Kustomize 根目录解析 `./data`，不受当前工作目录影响。它会核实可写的 Docker bind mount，包括仓库根目录等上级目录的挂载，并限制 PV 只在能访问该目录的节点使用。这些节点内的挂载目标路径必须一致。远程 Docker 的文件系统不属于客户端本机，此路径不接受远程 Docker endpoint。重建 k3d 时需要重新配置 bind mount，模型文件仍保留在宿主目录中。

### 其他 Kubernetes 集群

填写管理员已经在目标节点准备好的绝对目录：

```yaml
spec:
  directory: /srv/foretoken/data
  accessMode: ReadWriteMany
```

单节点集群会将 PV 固定到该节点。多节点集群中，这个声明表示同一个共享文件系统已经在**所有节点**按此路径挂载，并具有工作负载所需权限。只有路径相同不能证明目录共享；不满足此条件时应使用共享文件系统或普通存储 PVC。CLI 不上传客户端文件、不安装共享文件系统，也不验证远端文件系统身份。

除常规 namespace 权限外，部署用户还需有读取节点、创建和读取静态 PersistentVolume 的权限；namespaced PVC 仍由控制器管理。单独用 `kubectl apply` 提交 RuntimeCache 不会准备 PV，目录声明应通过 `foretoken deploy` 部署。有些集群准入策略禁止 hostPath，应选择动态存储而不是放宽集群策略。

## 加载目录中的模型

空目录可以直接配合 `Qwen/Qwen3-0.6B` 这类 Hub 标识使用。推理引擎和 frontend 按各自既有缓存布局下载文件，所有缓存位于挂载目录内；后续 Pod 会复用已下载文件，上游检查修订时仍可能联网。

使用已有 checkpoint 时，将完整模型目录放到 `data` 下：

```text
examples/quickstart/data/checkpointA/A3/
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── model.safetensors
```

将 `ModelService.spec.model` 设置为 `checkpointA/A3`。model-server 和 frontend 都会相对数据根目录定位文件，对外请求仍使用 `checkpointA/A3` 作为模型 ID。模型格式与 tokenizer 的有效性由推理引擎校验；单独配置的 tokenizer 目录不需要模型权重。已存在的单个 checkpoint 文件会明确报错，不会悄悄改为加载父目录。相对路径和软链接不能越过数据根目录。显式绝对模型目录仍按原来的 Pod 内路径解释。

## 动态 PVC 存储

需要集群创建存储卷时，删除 `directory` 并选择 StorageClass：

```yaml
spec:
  initialSize: 10Gi
  storageClassName: shared-storage
  accessMode: ReadWriteMany
```

省略 `storageClassName` 使用集群默认值。`initialSize` 是实际申请容量，很多存储驱动会按该容量创建并限制存储卷。K3s `local-path` 不实施目录容量上限，其实际空间取决于节点文件系统；不能将这个行为推广到所有 CSI 块卷或网络卷。

只有驱动支持在线卷和文件系统扩容时才设置 `maxSize`。Foretoken 会在各挂载点观测到的最低空闲比例达到 20% 时申请扩容，将请求容量逐次翻倍直到 `maxSize`。该上限只能增大，不能移除或减小；PVC 不支持缩容。正在进行的下载仍可能在扩容结束前耗尽空间。

## 保留、重建与恢复

两种模式均默认 `retentionPolicy: Retain`。删除目录模式部署会保留文件和 PV；如果同时删除 Namespace，Kubernetes 仍会删除其中的 PVC 对象。再次部署相同 namespace/cache 名称时会复用未变化的目录绑定，包括其原 PVC 已删除的保留 PV。CLI 不接管其他 owner、目录或节点位置的现有 PV；更换目录请使用新的 cache 名称，不要把现有 claim 指向其他数据。

显式设置 `retentionPolicy: Delete` 时，目录模式会在工作负载终止后删除 PVC 和 CLI 创建的 PV 对象，但仍保留目录文件。只有不再需要文件时才单独清理。动态 PVC 删除后是否删除底层数据，由 StorageClass 的回收策略决定。

model-server 启动过程中如果持久缓存不可写，Foretoken 会停止失败的 EngineCore，改用 Pod 临时缓存重试；frontend 缺少 Hub 缓存文件时也可以下载到临时卷。这不会把预先放置的本地 checkpoint 迁移到临时存储，也不能使尚未挂载的 PVC 可用。临时缓存会随 Pod 删除。

管理员也可以在平台 values 中设置 `workload.cache.claimName`，挂载已有 PVC。Foretoken 不创建、扩容或删除该 claim；所有使用这组配置的 workload namespace 都必须提供同名 PVC。

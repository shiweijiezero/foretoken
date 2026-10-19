<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型存储

[English](model-storage.md)

在部署的 `cache.yaml` 中选择数据目录。快速开始示例使用仓库的 `data/`：

```yaml
spec:
  directory: ../../../data
  accessMode: ReadWriteMany
```

然后正常部署：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

挂载同一目录的部署实例会复用下载的权重、tokenizer 和模型配置文件。同一命名空间中的实例也分别保存编译缓存、运行配置和性能剖析结果，不混用可写状态。

| 目录 | 用途 |
|---|---|
| `data/shared/models/` | 各服务共享的托管模型下载缓存 |
| `data/deployments/` | 各实例独立的运行状态与性能剖析目录 |
| `data/models/` | 自行准备和维护的模型文件，不参与下载缓存回收 |

源码构建缓存由构建流程单独管理。选择 ModelScope 或 Hugging Face 兼容地址，见[模型来源](model-sources_zh.md)。

## 选择目录

相对路径以 Kustomize 目录为基准。在 Git worktree 中，相对路径如果指向仓库默认的 `data/`，会使用主 checkout 的 `data/`，让各候选复用模型下载。显式绝对路径保持原来的目标。

本机 k3d 在 CLI 创建集群时准备仓库数据目录的挂载，操作见 [k3d 指南](../deployment/k3d_zh.md)。远程集群中，运行服务的节点必须能访问所选目录；跨节点共享应使用共享文件系统。指定 `directory` 就表示使用该目录，不会被静默替换为另一张独立动态卷。

已绑定的 RuntimeCache 更换目录时，需要使用新的 RuntimeCache 名称。自备模型需要迁移时，另行移动或复制其文件。

## 跨命名空间复用模型文件

管理员在安装平台时授权共享目录。从仓库根目录安装时，命令可以发现 CLI 主机与集群节点都能访问的默认 NFS `data/`；本机 k3d 则复用已有的目录绑定挂载。

成员继续在 `cache.yaml` 中选择该目录，使用正常部署命令。某个实例首次需要目录时，平台会准备所在命名空间的存储绑定，成员无需填写 PVC 名称或取得集群级存储权限。[命名空间服务权限](../../cli/README_zh.md#为成员准备命名空间)由管理员另外分配。

CLI 主机无法直接读取共享挂载时，管理员也可以用已有的 NFS `ReadWriteMany` 卷提供目录。将下面的配置保存到 `deploy/member-storage.yaml`，目录和源 PVC 换成实际选择：

```yaml
workload:
  cache:
    directories:
      models:
        directory: /srv/shared/foretoken-data
        sourceClaim: model-library/models
```

安装平台时，在原命令中加上 `--values deploy/member-storage.yaml`；源码安装保留 `-e` 和镜像仓库选项。

成员在 `spec.directory` 中填写 `/srv/shared/foretoken-data`。源 PVC 支持原生 NFS 和 `nfs.csi.k8s.io`。仅存在于 CLI 主机本地磁盘的目录无法提供远程共享；此时应选择节点可访问的目录，或使用下文的动态存储。

## 暂停、删除与复用

`foretoken stop` 释放服务运行资源，但保留其模型缓存引用；之后执行 `foretoken deploy` 可以继续复用文件。

`foretoken delete` 在选中实例的工作负载退出后释放模型引用。每个托管模型版本在该目录中不再被任何服务引用时，才会被回收。例如 A、B 两个实例使用 X，C、D 使用 Y：删除 A、B 后就能回收 X，无需等待 Y 停止。
数据目录本身、`data/models/` 中的自备模型和实例运行文件会保留。管理员准备的卷仍可用于后续部署；动态卷则遵循配置的保留策略及 StorageClass 回收策略。

## 使用已有模型

将完整模型目录放到数据根目录下：

```text
data/models/checkpointA/A3/
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── model.safetensors
```

在 `model.yaml` 中选择它：

```yaml
spec:
  model: checkpointA/A3
  source: local
```

model-server 和 frontend 会在 `data/models` 下解析该标识。只有 tokenizer 的目录可以放在 `data/models/tokenizers` 下，模型格式由推理引擎校验。这些自行维护的文件不随下载缓存一起回收。自备模型目录应放在 `data/shared/models` 之外；使用托管下载时，通过 `hf` 或 `modelscope` 选择模型。

## 使用动态存储

希望由集群申请存储，而非使用指定文件系统目录时，去掉 `directory`：

```yaml
spec:
  initialSize: 10Gi
  accessMode: ReadWriteMany
```

StorageClass 只支持单节点卷时，使用 `ReadWriteOnce`。需要指定存储类时添加 `storageClassName`；存储驱动支持在线扩容时，才设置 `maxSize`。分别申请的动态卷不会仅因为 StorageClass 相同就共用文件。

管理员也可以通过 `workload.cache.claimName` 将已有的命名空间 PVC 设为默认存储。成员显式选择已授权的 `directory` 时，以目录选择为准。

性能剖析结果保存在对应实例的私有数据区域，采集和查看方法见[性能剖析指南](../../benchmarks/docs/profile/README_zh.md)。

## 在节点间分发模型

`foretoken install` 会自动准备或复用共享下载服务。配置了持久存储的模型优先使用已有文件，缺失时才下载。Hugging Face、ModelScope 官方服务中可匿名访问的模型，在共享下载服务可用时使用节点间共享；服务不可用、模型需要身份认证或使用自定义地址时，直接从模型源下载。

兼容副本的运行环境具备 CUDA 和 RDMA 能力时，会自动复用运行中副本已加载的权重；不适用时，从准备好的模型文件加载。

`foretoken uninstall` 会在工作负载和控制器都不再使用时移除托管的下载服务；独立安装的服务由原有生命周期管理。

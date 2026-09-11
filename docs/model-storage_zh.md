<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型存储

[English](model-storage.md) | 简体中文

将下载的模型和运行时缓存放在同一个目录，供后续 Pod 复用。目录缓存使用[当前源码的 CLI 和平台](custom-deployment_zh.md)；已发布的 0.0.2 示例使用动态 PVC。

## 使用数据目录

源码示例在 `cache.yaml` 中声明数据目录：

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: RuntimeCache
metadata:
  name: models
spec:
  directory: ./data
  accessMode: ReadWriteMany
```

本机 k3d 的 `./data` 相对示例的 Kustomize 目录解析。在创建集群前准备目录，并将它绑定到运行模型和 frontend 的节点；[k3d 指南](k3d-deployment_zh.md)包含这部分挂载。目录需允许 Pod 运行用户写入，标准 frontend 使用 UID/GID 65532。

其他 Kubernetes 集群填写节点上的绝对路径：

```yaml
spec:
  directory: /srv/foretoken/data
  accessMode: ReadWriteMany
```

单节点集群可以使用本地目录。多节点集群需在每个节点的此路径挂载同一个共享文件系统。部署命令使用已有目录，不会从 CLI 所在机器上传文件。目录模式需要读取节点和创建静态 PersistentVolume 的权限。

目录容量由底层文件系统和配额决定，不与 `initialSize`、`maxSize`、`storageClassName` 同时配置。

准备好存储后，从仓库根目录部署：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

`data` 可以为空，首次使用时会下载模型。后续部署复用已有文件，模型来源服务仍可能检查更新。Frontend 和 model-server 共用这份数据目录。

## 加载本地模型

将完整模型目录放在 `data` 下，例如：

```text
examples/quickstart/data/checkpointA/A3/
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── model.safetensors
```

在 `model.yaml` 中设置：

```yaml
spec:
  model: checkpointA/A3
```

API 请求中的模型名称也使用 `checkpointA/A3`。模型格式及必需的 tokenizer 文件遵循推理引擎的加载要求；单个 checkpoint 文件需要先整理成支持的模型目录。相对路径和软链接不能越过 `data`；绝对模型路径指 Pod 内的目录。

## 使用 StorageClass

由 Kubernetes 创建存储时，将 `cache.yaml` 中的 `directory` 换成申请容量：

```yaml
spec:
  initialSize: 10Gi
  accessMode: ReadWriteMany
```

默认使用集群的 StorageClass，也可以通过 `storageClassName` 选择其他存储。多节点部署需要支持 `ReadWriteMany` 的存储。

驱动支持在线扩容时，可以添加 `maxSize`，例如 `100Gi`。后续可以增大，但不能移除或减小。存储驱动可能按申请容量限制卷大小；K3s `local-path` 则使用底层文件系统的可用空间。

## 保留与清理

目录模式在删除服务或 Pod 后保留文件。重建 k3d 集群时保留 `data`，并恢复 bind mount；重新部署同一示例即可复用数据。

`retentionPolicy: Delete` 会删除目录模式的 PV/PVC 对象，不删除文件。不再需要时再单独清理文件。动态 PVC 删除后是否删除底层数据，由 StorageClass 的回收策略决定。

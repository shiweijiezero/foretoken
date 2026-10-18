<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型存储

[English](model-storage.md)

两个 Quick Start 示例共用项目根目录的 `data/` 存放模型和运行时缓存。各自的 `cache.yaml` 使用：

```yaml
spec:
  directory: ../../../data
  accessMode: ReadWriteMany
```

然后直接部署：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

Foretoken 会为该配置创建目录型存储。删除服务时目录会保留，后续部署可以继续复用其中的文件。选择 ModelScope 或 Hugging Face 兼容地址见[模型来源](model-sources_zh.md)。

## 选择目录位置

本机 k3d 的相对路径以 Kustomize 根目录为基准。两个示例都用 `../../../data` 指向项目根目录的 `data/`，创建集群时将该目录 bind mount 到节点。请参阅 [k3d 指南](../deployment/k3d_zh.md)。

已有部署可以继续使用原路径。已绑定缓存更换目录时需要使用新的 RuntimeCache 名称；已有文件按需另行移动或复制。

远程集群会自动将 `../../../data` 这样的相对目录转换为动态 PVC，因此 Quick Start 的 YAML 无需修改。多节点集群使用 `ReadWriteMany` 共享模型文件和运行时数据；单节点集群会自动使用 `ReadWriteOnce`。需要选择特定 StorageClass、容量、访问模式或最大容量时，在 `cache.yaml` 中设置对应字段；绝对 `directory` 仍表示目标节点或所有目标节点共享文件系统上已经存在的路径。

## 使用已有模型

将完整模型目录放在数据根目录下：

```text
data/models/checkpointA/A3/
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── model.safetensors
```

在 `model.yaml` 中填写对外使用的模型标识：

```yaml
spec:
  model: checkpointA/A3
  source: local
```

model-server 和 frontend 会在 `data/models` 下解析该标识。只有 tokenizer 的目录可以放在 `data/models/tokenizers` 下；模型格式由推理引擎校验。

## 使用动态存储

集群需要动态创建 PVC 时，去掉 `directory`：

```yaml
spec:
  initialSize: 10Gi
  accessMode: ReadWriteMany
```

使用 `storageClassName` 选择 StorageClass。只有存储驱动支持在线扩容时，才增加 `maxSize`。

需要挂载其他系统管理的 PVC 时，在平台 values 中设置 `workload.cache.claimName`，并在每个工作负载命名空间创建该 PVC。

诊断采集复用同一数据根目录，并将结果写入 `profiles/`；操作方法见[性能剖析指南](../../benchmarks/docs/profile/README_zh.md)。

## 在节点间分发模型

Dragonfly 可以在节点间共享公开模型文件的下载。在 `deploy/platform-values.yaml` 中启用：

```yaml
modelDistribution:
  dragonfly:
    enabled: true
```

安装命令会准备 Dragonfly，或复用已有安装。需要身份认证的模型及自定义模型源仍直接从源站下载。存在多个 Dragonfly release 时，在 `modelDistribution.dragonfly` 下通过 `existingRelease: {name: dragonfly, namespace: dragonfly-system}` 选择一个，替换为实际的名称和命名空间。

在具备 RDMA 的 NVIDIA 集群上，ModelExpress 可以从运行中的副本加载权重。同时启用两项能力时，将下面的设置放在同一个 `modelDistribution` 下：

```yaml
modelDistribution:
  modelexpress:
    enabled: true
```

自动权重传输适用于使用持久缓存、数据并行度为 1、未启用专家动态负载均衡的远端模型。显式设置的 `load-format` 保持不变；没有兼容源副本时，加载已准备的文件。

在原安装命令中添加 `--values deploy/platform-values.yaml` 应用配置。源码安装保留 `-e .`、镜像仓库设置和已有的引擎源码绑定。使用发布版平台时：

```bash
foretoken install --values deploy/platform-values.yaml
```

修改这些设置后，重新部署需要更新的服务。将对应的 `enabled` 设为 `false` 可关闭分发。卸载时，没有工作负载使用的托管 Dragonfly 会被移除，复用的安装会保留。

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

`foretoken install` 会自动准备或复用共享模型下载服务。配置了持久存储的模型优先使用已有缓存，缺失的文件才需要下载。Hugging Face、ModelScope 官方服务中可匿名访问的模型可以通过节点间共享下载获取；需要身份认证或使用自定义地址时，直接从模型源下载。

### 从运行中的副本加载权重

在具备 RDMA 的 NVIDIA 集群上，还可以启用 ModelExpress，从运行中的副本加载权重。在 `deploy/platform-values.yaml` 中配置：

```yaml
modelDistribution:
  modelexpress:
    enabled: true
```

权重传输适用于使用持久缓存、数据并行度为 1、未启用专家动态负载均衡的远端模型。显式设置的 `load-format` 保持不变；没有兼容源副本时，加载已准备的文件。将 `modelexpress.enabled` 设为 `false` 可关闭权重传输。

### 自定义共享下载

安装命令通过 Dragonfly 管理共享下载。命名空间的 `pod-security.kubernetes.io/enforce` 标签为 `baseline` 或 `restricted` 时，自动停用节点间下载。

需要统一使用直连下载，或集群通过其他策略禁止 `hostPath` 挂载时，配置：

```yaml
modelDistribution:
  dragonfly:
    enabled: false
```

设为 `enabled: true` 可恢复共享下载。集群中有多个 Dragonfly 安装时，将 `modelDistribution.dragonfly.existingRelease` 设为 `{name: dragonfly, namespace: dragonfly-system}`，填写要复用的 Helm release 名称和命名空间。

在原安装命令中添加 `--values deploy/platform-values.yaml` 应用自定义配置。源码安装保留 `-e .`、镜像仓库设置和已有的引擎源码绑定。使用发布版平台时：

```bash
foretoken install --values deploy/platform-values.yaml
```

修改配置后，重新部署需要更新的服务。`foretoken uninstall` 会移除已无工作负载使用的托管 Dragonfly，复用的安装会保留。

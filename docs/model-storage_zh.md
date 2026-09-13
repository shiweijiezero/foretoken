<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型存储

[English](model-storage.md)

模型下载文件和运行时缓存默认放在示例的数据目录中：

```yaml
spec:
  directory: ./data
  accessMode: ReadWriteMany
```

然后直接部署：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

Foretoken 会为该配置创建目录型存储。删除服务时目录会保留，后续部署可以继续复用其中的文件。

## 选择目录位置

使用本机 k3d 时，在创建集群前把示例的 `data` 目录 bind mount 到节点。请参阅 [k3d 指南](k3d-deployment_zh.md)。

使用远程集群时，填写目标节点已经准备好的绝对路径，或填写所有目标节点都能访问的同一共享文件系统路径。客户端本地的 `./data` 不会自动上传。

## 使用已有模型

将完整模型目录放在数据根目录下：

```text
examples/quickstart/data/models/checkpointA/A3/
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── model.safetensors
```

在 `model.yaml` 中填写对外使用的模型标识：

```yaml
spec:
  model: checkpointA/A3
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

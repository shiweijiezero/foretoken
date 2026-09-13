<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型来源

[English](model-sources.md)

Foretoken 从同一来源加载模型权重，以及 frontend 需要的 tokenizer、配置和对话模板。未设置模型来源时使用 Hugging Face Hub，默认仍执行 `foretoken install`。

## 选择远端来源

使用 Hugging Face 兼容地址时，创建 `model-source-values.yaml`：

```yaml
runtime:
  vllm:
    modelSource:
      endpoint: https://hub.example.com
```

使用 ModelScope 时改为：

```yaml
runtime:
  vllm:
    modelSource:
      provider: modelscope
```

使用所选配置安装平台：

```bash
foretoken install --values model-source-values.yaml
```

`endpoint` 只适用于 Hugging Face 来源。仓库访问或下载失败时，模型启动会直接报错，不会切换到其他来源。

使用相同的公开模型标识部署维护中的示例：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

## 使用本地模型目录

统一模型根目录下的完整目录优先于远端来源。将模型文件放到示例数据目录：

```text
examples/quickstart/data/models/checkpointA/A3/
```

在 `ModelService` 中使用相同的相对标识：

```yaml
spec:
  model: checkpointA/A3
```

使用上面的部署命令即可。model-server 和 frontend 会复用该目录，公开模型标识保持不变。目录存储和 PVC 配置见[模型存储](model-storage_zh.md)。

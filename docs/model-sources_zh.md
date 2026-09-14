<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型来源

[English](model-sources.md)

每个 `ModelService` 分别选择模型、tokenizer、配置和对话模板的来源，默认使用 Hugging Face Hub。

```yaml
spec:
  model: Qwen/Qwen3-0.6B
  source: hf # 支持 local、hf、modelscope，默认为 hf。
```

使用 `source: modelscope` 时，相同模型标识会从 ModelScope 加载。同一个 frontend 后面的不同模型服务可以选择不同来源。

## 使用本地模型目录

将完整模型放到统一模型根目录：

```text
examples/quickstart/data/models/checkpointA/A3/
```

选择本地来源，并继续用相对路径作为公开模型标识：

```yaml
spec:
  model: checkpointA/A3
  source: local
```

也可以使用 frontend 和 model-server Pod 中都已挂载的绝对目录。本地目录不存在或远端下载失败时，该模型不会就绪，Foretoken 不会切换来源。

正常部署配置：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

目录存储和 PVC 配置见[模型存储](model-storage_zh.md)。

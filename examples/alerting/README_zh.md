<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 告警示例

[English](README.md) | 简体中文

在指标采集之外单独启用 Foretoken 告警规则。在仓库根目录运行：

```bash
foretoken install --values examples/alerting/platform.yaml
```

在同一命令中继续传入已有的平台配置。修改本示例即可调整阈值或 `alerting.language`，详见[告警指南](../../observability/alerting_zh.md)。设置 `alerting.enabled: false` 并再次安装，只移除告警规则，不移除指标和 Dashboard。

`lark/` Kustomize overlay 将可选接收器接入已有 Alertmanager。先创建 webhook Secret，再按 [Lark 指南](../../observability/integrations/lark/README_zh.md)配置接收器选择和触发、解除通知验证。

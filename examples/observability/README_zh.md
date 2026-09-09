<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 指标采集与 Dashboard

[English](README.md) | 简体中文

通过 `platform.yaml` 配置 Foretoken 指标采集和 Grafana Dashboard，无需修改 Chart。在仓库根目录运行：

```bash
foretoken install --values examples/observability/platform.yaml
foretoken deploy examples/quickstart
```

已有模型部署无需重新部署。安装命令应继续携带已有的硬件或镜像配置文件；要使用当前源码中的镜像和 Chart，在 `foretoken install` 中增加 `-e .`。

按照[可观测性指南](../../observability/README_zh.md)打开 Prometheus 和 Grafana。告警通过 [`examples/alerting`](../alerting/README_zh.md) 单独配置。

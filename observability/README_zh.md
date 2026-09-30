<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性

[English](README.md) | 简体中文

通过 Grafana 查看服务性能、查询持久日志并分析告警。安装平台时默认启用指标与日志采集，告警按需选择。首次部署服务见[快速开始](../README_zh.md#快速开始)。

## 查看指标

从集群的监控入口打开 Grafana。Foretoken 托管的 Grafana Service 位于 `foretoken-platform` 命名空间，名称为 `foretoken-prometheus-grafana`，端口为 80，默认类型是 `ClusterIP`；集群外访问需要由集群管理员配置入口。复用 Grafana 时沿用已有访问方式。

打开 Foretoken 系统概览，或英文版 Foretoken System Overview。先选择命名空间和模型，再按模型实例、执行角色、引擎编号（rank）查看后端明细。模型总量曲线始终汇总全部实例，明细曲线随这些筛选变化。

默认查看最近 15 分钟，调整时间范围可查看历史趋势；概览数值对应所选范围的终点。上报端点数反映指标采集情况，不代表服务就绪。

| 要判断的问题 | 重点查看 |
| --- | --- |
| 模型能否跟上请求负载？ | 输入／输出 token 速率、完成请求速率，以及运行和等待队列。 |
| 响应慢在哪个阶段？ | 首 token 和整体生成延迟、输出 token 间隔，以及排队／预填充／解码耗时。 |
| 推测解码是否有效？ | 启用该能力后，结合草稿接受率、每次草稿迭代接受 token 数、输出吞吐和延迟判断。 |
| 缓存或设备是否紧张？ | 缓存占用和命中率、文件系统空间、GPU 利用率与显存，以及 CPU／内存用量。 |
| 路由与副本配置是否合适？ | 各模型、执行角色内的路由选择份额，以及扩缩容建议与实际副本数。 |

TTFT 表示首 token 延迟，E2EL 计至生成完成，两者使用秒。TPOT 统计每个请求的平均输出 token 间隔，ITL 统计逐 token 间隔，两者使用毫秒并提供均值曲线。详细统计口径见各面板说明。

“共享前端”区域涵盖所选前端服务的全部模型流量，统计 HTTP 响应开始事件。“控制面”展示平台状态；扩缩容区域按所选模型和扩缩容服务展示。

## 查询日志

在 Grafana 的探索页面（Explore）选择 Foretoken Logs 数据源和时间范围。例如查询快速开始命名空间的日志：

```logql
{job="foretoken", namespace="foretoken-demo"}
```

用 `pod`、`container`、`node` 或 `stream` 缩小范围。在查询末尾加 `|~ "(?i)error"` 查找错误，或加 `|= "request-id"` 并替换为要追踪的请求标识。

采集范围包括模型服务及其推理引擎、前端、KV 服务和控制器。服务 Pod 或其命名空间删除后，已采集的日志仍可查询。

## 告警

在所属的 `ModelService` 或 `FrontendService` 中选择规则。例如，在 `spec` 下启用指标抓取失败告警：

```yaml
observability:
  alerts:
    rules:
      - ForetokenMetricsTargetDown
```

修改后重新部署服务配置即可生效。移除规则或设为 `rules: []`，再次部署后关闭告警，指标仍保留。[服务可观测性示例](../examples/observability/README_zh.md)提供可运行的配置和部署命令。

告警名称、阈值及适用的服务类型见[告警参考](runbooks/alerts_zh.md)。需要接收通知时，配置 [Lark](integrations/lark/README_zh.md)、[Slack](integrations/slack/README_zh.md) 或[钉钉](integrations/dingtalk/README_zh.md)接收器。

## 平台设置

下面的 `foretoken install` 命令也用于更新已有安装。源码安装需保留 `-e .`，并从源码根目录执行。升级 Foretoken 后，重新执行原安装命令，让看板和指标采集一起更新。

### Grafana 登录

托管 Grafana 默认允许免登录查看。需要登录后才能查看看板和日志时，执行：

```bash
foretoken install --grafana-auth password
```

使用 `--grafana-auth anonymous` 恢复免登录查看；后续安装会保留选择。复用的 Grafana 仍由原平台管理员管理。

获取首次安装时生成的管理员账号和密码：

```bash
kubectl get secret --namespace foretoken-platform \
  foretoken-prometheus-grafana --output json \
  | python3 -c 'import base64,json,sys; d=json.load(sys.stdin)["data"]; print("User:",base64.b64decode(d["admin-user"]).decode()); print("Password:",base64.b64decode(d["admin-password"]).decode())'
```

如果已在 Grafana 中改过密码，使用修改后的密码。

### 日志存储

托管日志默认保留 14 天，通过默认 StorageClass 初始申请 5 GiB 存储。如需保留 30 天，并允许容量增长到 50 GiB，在 `platform-values.yaml` 中填写：

```yaml
observability:
  logs:
    retention: 720h
    maxSize: 50Gi
```

修改文件后重新应用：

```bash
foretoken install --values platform-values.yaml
```

设置 `maxSize` 后，用量达到 80% 时自动将申请容量翻倍，直到上限。存储驱动需支持在线扩容和按卷统计用量。

| `observability.logs` 下的选项 | 用途 |
| --- | --- |
| `storageClass` / `initialSize` | 新建存储使用的 StorageClass 和初始容量；已有卷保留当前容量。 |
| `endpoint` | 填写采集器和 Grafana 可访问的现有 Loki HTTP(S) 基础地址。 |
| `enabled: false` | 停止托管采集，历史日志仍可查询。 |

### 复用已有监控

安装时会复用兼容的 Prometheus 和 GPU exporter。存在多个 Prometheus 时，显式选择一个。以下以 `monitoring/prometheus` 为例，需替换为实际的命名空间和实例名称：

```bash
kubectl label namespace monitoring inference.foretoken.io/metrics-scraper=true --overwrite
foretoken install --prometheus monitoring/prometheus
```

所选 Prometheus 需要选中 `foretoken-platform` 中的抓取配置（ServiceMonitor）和记录规则。启用服务告警时，`ruleNamespaceSelector` 还需包含工作负载的命名空间。GPU exporter 需覆盖 GPU 节点，并提供 Pod 和命名空间标签，以关联模型工作负载。

需要 Grafana 自动加载配置时，分别发现 `foretoken-platform` 中带有 `grafana_dashboard=1` 标签的看板 ConfigMap，以及带有 `grafana_datasource=1` 标签的数据源 ConfigMap。手工导入中文看板时，导出已安装的版本：

```bash
kubectl get configmap --namespace foretoken-platform \
  foretoken-control-plane-system-dashboard \
  --output jsonpath='{.data.foretoken-system-overview-zh\.json}' \
  > /tmp/foretoken-system-overview-zh.json
```

在 Grafana 中导入该文件，并选择对应的 Prometheus 数据源。日志查询另需添加名为 Foretoken Logs、指向所配置地址的 Loki 数据源；托管 Loki 的地址是 `http://foretoken-loki.foretoken-platform.svc:3100`。

## 没有数据显示时

先核对命名空间、模型和时间范围，并向服务发送请求。在 Prometheus 的 Targets 页面检查抓取失败，在 Rules 页面检查 `foretoken.recording` 规则组。查看已安装的抓取配置和规则：

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

已触发的告警按[告警参考](runbooks/alerts_zh.md)定位。需要进一步分析模型进程内的执行耗时时，使用[性能剖析](../benchmarks/docs/profile/README_zh.md)。

## 清理

先删除模型和前端服务，再执行 `foretoken uninstall`。卸载会删除平台及其托管的监控、采集器和 Loki，保留日志存储和复用的安装。使用原安装命令与日志配置重新安装后，可恢复查询保留的日志。

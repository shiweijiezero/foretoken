# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Builds the Foretoken System Overview dashboard shipped by the Helm chart.

Run `make dashboard` from the repository root after changing this file. It writes the English
and Chinese JSON dashboards under `deploy/charts/foretoken/files/grafana/`; the chart installs
both through one Grafana ConfigMap. The generated JSON files are deployed artifacts, and this
module is their shared source.

The dashboard reads raw Frontend and model-server metrics over a selected rate interval,
plus recording rules and bounded router, controller, and autoscaling metrics. Its section order
follows the Dynamo dashboard: service health first, then the request path from the Frontend through model
serving and caches to accelerators, and finally routing, control-plane, and autoscaling decisions.
"""

from __future__ import annotations

import argparse
import json

from grafana_foundation_sdk.builders import common, dashboard, heatmap, prometheus, stat, table, timeseries
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models import common as models
from grafana_foundation_sdk.models import dashboard as dashboard_models
from grafana_foundation_sdk.models import heatmap as heatmap_models
from grafana_foundation_sdk.models import prometheus as prometheus_models

PROMETHEUS = dashboard_models.DataSourceRef(type_val="prometheus", uid="${DS_PROMETHEUS}")

# Requests are blue, tokens orange, errors red, caches teal; latency quantiles darken with rank.
GREEN = "#0ca30c"
AMBER = "#eda100"
RED = "#d03b3b"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
TEAL = "#1baf7a"
QUANTILE_COLORS = {"p50": "#86b6ef", "p90": "#3987e5", "p99": "#184f95"}
STAGE_COLORS = {"queue": AMBER, "prefill": BLUE, "decode": TEAL}

# Exact UI-string translations keep both dashboards on one query and layout definition.
ZH = {
    "Foretoken System Overview": "Foretoken 系统概览",
    "Overview": "概览",
    "Frontend": "Frontend",
    "Model Serving": "模型服务",
    "Cache": "缓存",
    "Accelerators and Resources": "加速器与资源",
    "Routing decisions": "路由决策",
    "Control plane": "控制面",
    "Autoscaling decisions": "扩缩容决策",
    "Frontend targets": "Frontend 采集目标",
    "Model servers": "模型服务器",
    "Requests / s": "请求 / s",
    "5xx ratio": "5xx 比例",
    "Prompt tokens / s": "Prompt token / s",
    "Output tokens / s": "输出 token / s",
    "Queued requests": "排队请求",
    "Temporary RuntimeCache": "临时 RuntimeCache",
    "Request rate by status": "按状态统计的请求速率",
    "Request rate by endpoint": "按端点统计的请求速率",
    "Response-start latency": "响应开始延迟",
    "Admission queue": "准入队列",
    "Completed request rate": "完成请求速率",
    "Token throughput": "Token 吞吐量",
    "Scheduler state": "调度器状态",
    "End-to-end latency (E2EL)": "端到端延迟 (E2EL)",
    "Time to first token (TTFT)": "首 token 延迟 (TTFT)",
    "Time per output token (TPOT)": "每输出 token 耗时 (TPOT)",
    "Inter-token latency (ITL)": "Token 间延迟 (ITL)",
    "Request time by stage": "各阶段请求耗时",
    "Preemptions": "抢占",
    "Prompt length": "Prompt 长度",
    "Output length": "输出长度",
    "KV Cache utilization": "KV Cache 使用率",
    "Prefix Cache hit ratio": "Prefix Cache 命中率",
    "KV index source health": "KV 索引数据源健康度",
    "RuntimeCache utilization": "RuntimeCache 使用率",
    "RuntimeCache available space": "RuntimeCache 可用空间",
    "Node mean GPU utilization": "节点平均 GPU 使用率",
    "Node mean GPU memory utilization": "节点平均 GPU 显存使用率",
    "GPU utilization by device": "各设备 GPU 使用率",
    "GPU memory by device": "各设备 GPU 显存使用率",
    "GPU power by device": "各设备 GPU 功耗",
    "GPU temperature by device": "各设备 GPU 温度",
    "Serving CPU usage": "服务 CPU 使用量",
    "Serving memory usage": "服务内存使用量",
    "Routing outcomes": "路由结果",
    "Routing stage latency": "路由阶段延迟",
    "Routing candidates": "路由候选数",
    "Reconcile errors": "Reconcile 错误",
    "Reconcile latency": "Reconcile 延迟",
    "Controller workqueues": "控制器工作队列",
    "Replica decisions": "副本决策",
    "Serving capacity": "服务容量",
    "Observation and evaluation age": "观测与评估数据时效",
    "Latest autoscaling stage": "最新扩缩容阶段",
    "Data source": "数据源",
    "Namespace": "命名空间",
    "Frontend service": "Frontend 服务",
    "Model group": "模型组",
    "Model role": "模型角色",
    "Model": "模型",
    "Model service": "模型服务",
    "Output": "输出",
    "Running": "运行中",
    "Waiting": "等待中",
    "mean": "平均值",
    "Not configured": "未配置",
    "recommendation / {{modelservice}} / {{target_name}} / {{role}}":
        "建议 / {{modelservice}} / {{target_name}} / {{role}}",
    "adjusted / {{modelservice}} / {{target_name}} / {{role}}":
        "调整后 / {{modelservice}} / {{target_name}} / {{role}}",
    "applied / {{modelservice}} / {{target_name}} / {{role}}":
        "已应用 / {{modelservice}} / {{target_name}} / {{role}}",
    "ready / {{modelservice}} / {{target_name}} / {{role}}":
        "就绪 / {{modelservice}} / {{target_name}} / {{role}}",
    "routable / {{modelservice}} / {{target_name}} / {{role}}":
        "可路由 / {{modelservice}} / {{target_name}} / {{role}}",
    "observation / {{modelservice}} / {{target_name}} / {{role}}":
        "观测 / {{modelservice}} / {{target_name}} / {{role}}",
    "evaluation / {{modelservice}} / {{target_name}} / {{role}}":
        "评估 / {{modelservice}} / {{target_name}} / {{role}}",
    "Prometheus targets currently reporting for the selected Frontend services.":
        "当前正在上报所选 Frontend 服务指标的 Prometheus target 数量。",
    "Prometheus targets currently reporting for the selected model groups and roles.":
        "当前正在上报所选模型组和角色指标的 Prometheus target 数量。",
    "Frontend responses started per second over the selected rate window.":
        "选定速率窗口内每秒开始的 Frontend 响应数。",
    "HTTP responses that started with 5xx divided by all started responses. Streaming failures after headers are not included.":
        "开始时状态为 5xx 的 HTTP 响应占全部已开始响应的比例，不包含响应头发出后的流式失败。",
    "Prompt tokens processed per second by the selected model servers.":
        "所选模型服务器每秒处理的 Prompt token 数。",
    "Generated tokens produced per second by the selected model servers.":
        "所选模型服务器每秒生成的输出 token 数。",
    "Requests waiting for frontend admission to a scaling target.":
        "正在等待 Frontend 准入到扩缩容目标的请求数。",
    "One when any selected model-server has fallen back to Pod-scoped temporary cache storage. No value means the selected groups do not use RuntimeCache.":
        "任一所选模型服务器回退到 Pod 范围的临时缓存时为 1；无值表示所选模型组未使用 RuntimeCache。",
    "Frontend response starts grouped by HTTP status class.": "按 HTTP 状态类别分组的 Frontend 响应开始速率。",
    "Frontend response starts grouped by HTTP endpoint.": "按 HTTP 端点分组的 Frontend 响应开始速率。",
    "Time, in seconds, until the Frontend handler produces HTTP response headers. This excludes SSE body delivery; it is neither TTFT nor full-stream duration.":
        "Frontend handler 生成 HTTP 响应头所需的秒数，不包含 SSE 正文传输，也不等同于 TTFT 或完整流持续时间。",
    "Requests waiting for runtime preparation or backend dispatch, grouped by scaling-target kind.":
        "按扩缩容目标类型分组，等待运行时准备或后端派发的请求数。",
    "Successfully completed model-server requests grouped by finish reason.":
        "按结束原因分组的模型服务器成功完成请求速率。",
    "Prompt and generated token throughput for the selected model servers.":
        "所选模型服务器的 Prompt 与输出 token 吞吐量。",
    "Requests running in vLLM execution batches or waiting in its scheduler.":
        "正在 vLLM 执行批次中运行或在调度器中等待的请求数。",
    "Maximum per-model-group quantile, in seconds, from Frontend handler entry after JSON decoding to the model-server terminal output. Excludes downstream client body consumption. Frontend and model-server clocks must be synchronized.":
        "从 JSON 解码后的 Frontend handler 入口到模型服务器终止输出的每模型组最大分位数，单位为秒；不包含下游客户端正文消费时间。Frontend 与模型服务器时钟必须同步。",
    "Maximum per-model-group TTFT quantile, in seconds, from Frontend handler entry after JSON decoding to the first token received by model-server. Chat and completion requests share this origin.":
        "从 JSON 解码后的 Frontend handler 入口到模型服务器收到首个 token 的每模型组最大 TTFT 分位数，单位为秒；Chat 与 Completion 请求使用相同起点。",
    "Maximum per-model-group request-level TPOT quantiles and mean over the selected rate window, in milliseconds. Each request contributes its average time between output tokens.":
        "选定速率窗口内每模型组最大的请求级 TPOT 分位数和平均值，单位为毫秒；每个请求贡献一次输出 token 间平均耗时。",
    "Maximum per-model-group gap between consecutive output-token events, in milliseconds. Unlike TPOT, each token interval is observed separately.":
        "每模型组相邻输出 token 事件间隔的最大分位数，单位为毫秒；与 TPOT 不同，每个 token 间隔都会单独观测。",
    "P90 time, in seconds, a request spends waiting for the scheduler, in prefill, and in decode.":
        "请求在等待调度器、Prefill 和 Decode 阶段的 P90 耗时，单位为秒。",
    "Requests preempted per second because KV-cache blocks ran out. Sustained preemption precedes the KV-cache pressure alert.":
        "因 KV Cache block 耗尽而每秒被抢占的请求数；持续抢占通常先于 KV Cache 压力告警。",
    "Distribution of prompt tokens per request over time.": "随时间变化的单请求 Prompt token 数分布。",
    "Distribution of generated tokens per request over time.": "随时间变化的单请求输出 token 数分布。",
    "Highest in-engine KV-cache utilization grouped by model role.":
        "按模型角色分组的引擎内最高 KV Cache 使用率。",
    "Average local and external Prefix Cache token hit ratios across selected model groups.":
        "所选模型组的本地与外部 Prefix Cache token 平均命中率。",
    "Healthy KV event sources divided by configured sources. Disabled or unavailable indexing reports zero.":
        "健康 KV 事件源数除以已配置源数；索引禁用或不可用时为 0。",
    "Mounted RuntimeCache filesystem utilization. Series are absent for model groups without a RuntimeCache.":
        "已挂载 RuntimeCache 文件系统的使用率；未配置 RuntimeCache 的模型组不会产生序列。",
    "Filesystem space available to model-server processes using a RuntimeCache.":
        "使用 RuntimeCache 的模型服务器进程可用文件系统空间。",
    "Mean utilization of Foretoken-attributed GPUs by vendor and node across workload namespaces. Shared devices are counted once; devices without Foretoken Pod attribution are excluded.":
        "跨工作负载命名空间按厂商和节点统计的 Foretoken GPU 平均使用率；共享设备只计一次，不包含无法关联到 Foretoken Pod 的设备。",
    "Mean memory utilization of Foretoken-attributed GPUs by vendor and node across workload namespaces. Shared devices are counted once; devices without Foretoken Pod attribution are excluded.":
        "跨工作负载命名空间按厂商和节点统计的 Foretoken GPU 平均显存使用率；共享设备只计一次，不包含无法关联到 Foretoken Pod 的设备。",
    "Utilization of each Foretoken-attributed GPU.": "每块可关联到 Foretoken 的 GPU 使用率。",
    "Memory utilization of each Foretoken-attributed GPU.": "每块可关联到 Foretoken 的 GPU 显存使用率。",
    "Power draw of each Foretoken-attributed NVIDIA GPU.": "每块可关联到 Foretoken 的 NVIDIA GPU 功耗。",
    "Temperature of each Foretoken-attributed NVIDIA GPU.": "每块可关联到 Foretoken 的 NVIDIA GPU 温度。",
    "CPU cores consumed by Frontend and model-server containers in the selected namespaces.":
        "所选命名空间中 Frontend 与模型服务器容器使用的 CPU 核数。",
    "Working-set memory used by Frontend and model-server containers in the selected namespaces.":
        "所选命名空间中 Frontend 与模型服务器容器使用的工作集内存。",
    "Selection results by workflow round. A selection failure is not an HTTP status; a request can involve several rounds.":
        "按工作流轮次统计的选择结果；选择失败不是 HTTP 状态，一个请求可能包含多轮选择。",
    "P99 filter, scorer and picker execution time, aggregated from histogram buckets across selected Frontend replicas.":
        "从所选 Frontend 副本直方图桶聚合得到的 Filter、Scorer 与 Picker P99 执行时间。",
    "Mean available, filtered and selectable candidate counts. Selectable counts include data-parallel ranks.":
        "可用、过滤后和可选择候选项的平均数量；可选择数量包含数据并行 rank。",
    "Controller-runtime errors per second. This section observes the platform controller independently of workload namespace filters.":
        "每秒 controller-runtime 错误数；本分区独立于工作负载命名空间筛选器观察平台控制器。",
    "P99 reconciliation time by controller; this is control-plane work, not inference request latency.":
        "按控制器统计的 P99 Reconcile 耗时，属于控制面工作而非推理请求延迟。",
    "Queued reconciliations. Current replica counts are deduplicated rather than added across controller replicas.":
        "排队中的 Reconcile 数；当前副本数会跨控制器副本去重，而不是相加。",
    "Latest published recommendation, adjusted target and applied desired capacity. A missing recommendation means the decision algorithm did not provide one.":
        "最新发布的建议值、调整后目标和已应用期望容量；建议值缺失表示决策算法未提供建议。",
    "Ready and routable capacity published by the autoscaler, compared with applied desired replicas.":
        "扩缩容器发布的 Ready 与可路由容量，并与已应用的期望副本数比较。",
    "Elapsed time since the last observation and evaluation; age keeps increasing if reconciliation stops. Missing observation series means no usable observation was published.":
        "距最近一次观测和评估的时间；Reconcile 停止后该时长会继续增加。观测序列缺失表示尚未发布可用观测。",
    "Current trigger, decision and adjustment outcomes from ModelService status. Reasons are bounded status codes, not log messages.":
        "ModelService status 中当前的触发、决策与调整结果；原因是有限状态码，而不是日志文本。",
}
AUTOSCALING_COLUMNS_ZH = {
    "namespace": "命名空间",
    "modelservice": "模型服务",
    "target_kind": "目标类型",
    "target_name": "目标名称",
    "role": "角色",
    "stage": "阶段",
    "algorithm": "算法",
    "disposition": "结果",
    "reason": "原因",
}

# Normalized selectors address recording rules and raw selectors address scrape-time metrics.
FRONTEND = 'namespace=~"$namespace",frontend_service=~"$frontend_service"'
GROUP = 'namespace=~"$namespace",model_group=~"$model_group",model_role=~"$model_role"'
MODEL = GROUP + ',model_name=~"$model_name"'
RAW_FRONTEND = (
    'endpoint="http",namespace=~"$namespace",inference_foretoken_io_frontend_service!="",'
    'inference_foretoken_io_frontend_service=~"$frontend_service"'
)
RAW_MODEL = (
    'endpoint="model-server",namespace=~"$namespace",inference_foretoken_io_model_group!="",'
    'inference_foretoken_io_model_group=~"$model_group",inference_foretoken_io_model_role!="",'
    'inference_foretoken_io_model_role=~"$model_role",model_name=~"$model_name"'
)
FRONTEND_DIMENSIONS = "namespace,frontend_service"
MODEL_DIMENSIONS = "namespace,model_group,model_role,pd_pipeline_scope,model_name"
ROUTER = 'namespace=~"$namespace",inference_foretoken_io_frontend_service=~"$frontend_service"'
SERVICE = 'namespace=~"$namespace",modelservice=~"$model_service"'
CONTROLLER = 'job="foretoken-control-plane"'
AUTOSCALING_TARGET = "namespace,modelservice,target_kind,target_name,role"
AUTOSCALING_LEGEND = "{{modelservice}} / {{target_name}} / {{role}}"
DEVICE_LEGEND = "{{node}} / {{device_id}}"

def query(expr: str, legend: str | None = None, *, interval: str | None = None) -> prometheus.Dataquery:
    target = prometheus.Dataquery().datasource(PROMETHEUS).expr(expr).range()
    if interval is not None:
        target.interval(interval)
    if legend is not None:
        target.legend_format(legend)
    return target


def foretoken_query(expr: str, legend: str | None = None) -> prometheus.Dataquery:
    """Query a Foretoken-owned target scraped every five seconds."""
    return query(expr, legend, interval="5s")


def _selector(metric: str, labels: str, extra: str) -> str:
    return f"{metric}{{{labels}{',' + extra if extra else ''}}}"


def frontend_metric(metric: str, *, extra: str = "", rate: bool = False) -> str:
    """Return one raw Frontend metric with its service label normalized for aggregation."""
    expr = _selector(metric, RAW_FRONTEND, extra)
    if rate:
        expr = f"rate({expr}[$__rate_interval])"
    return (
        f'label_replace({expr}, "frontend_service", "$1", '
        '"inference_foretoken_io_frontend_service", "(.+)")'
    )


def model_metric(metric: str, *, extra: str = "", rate: bool = False) -> str:
    """Return one raw model-server metric with workload identity labels normalized."""
    expr = _selector(metric, RAW_MODEL, extra)
    if rate:
        expr = f"rate({expr}[$__rate_interval])"
    return (
        f'label_replace(label_replace(label_replace({expr}, '
        '"model_group", "$1", "inference_foretoken_io_model_group", "(.+)"), '
        '"model_role", "$1", "inference_foretoken_io_model_role", "(.+)"), '
        '"pd_pipeline_scope", "$1", "inference_foretoken_io_pd_pipeline_scope", "(.+)")'
    )


def histogram_quantile(bucket_rates: str, dimensions: str, quantile: float) -> str:
    """Calculate a histogram quantile after preserving the metric's workload dimensions."""
    return f"histogram_quantile({quantile}, sum by({dimensions},le) ({bucket_rates}))"


def model_rate_ratio(numerator_metric: str, denominator_metric: str) -> str:
    """Average per-model-group ratios derived from raw counters over the selected rate window."""
    numerator = f"sum by({MODEL_DIMENSIONS}) ({model_metric(numerator_metric, rate=True)})"
    denominator = f"sum by({MODEL_DIMENSIONS}) ({model_metric(denominator_metric, rate=True)})"
    return f"avg({numerator} / clamp_min({denominator}, 1e-9))"


def steps(*thresholds: tuple[float | None, str]) -> dashboard.ThresholdsConfig:
    return dashboard.ThresholdsConfig().mode(dashboard_models.ThresholdsMode.ABSOLUTE).steps(
        [dashboard_models.Threshold(value=value, color=color) for value, color in thresholds]
    )


def fixed(color: str) -> dashboard.FieldColor:
    return dashboard.FieldColor().mode(dashboard_models.FieldColorModeId.FIXED).fixed_color(color)


def headline(
    title: str,
    description: str,
    expr: str,
    *,
    unit: str = "short",
    color: str | None = BLUE,
    thresholds: dashboard.ThresholdsConfig | None = None,
    no_value: str = "0",
    interval: str | None = None,
) -> stat.Panel:
    """A single-value tile in the Overview section, colored by threshold when one is given."""
    panel = (
        stat.Panel()
        .title(title)
        .description(description)
        .datasource(PROMETHEUS)
        .unit(unit)
        .no_value(no_value)
        .color_mode(models.BigValueColorMode.VALUE)
        .graph_mode(models.BigValueGraphMode.AREA)
        .reduce_options(common.ReduceDataOptions().calcs(["lastNotNull"]))
        .with_target(query(expr, title, interval=interval).ref_id("A"))
        .span(6)
        .height(4)
    )
    if color is None:
        panel.color_scheme(dashboard.FieldColor().mode(dashboard_models.FieldColorModeId.THRESHOLDS))
    else:
        panel.color_scheme(fixed(color))
    panel.thresholds(thresholds if thresholds is not None else steps((None, color)))
    return panel


def series(
    title: str,
    description: str,
    targets: list[prometheus.Dataquery],
    *,
    unit: str,
    span: int,
    colors: dict[str, str] | None = None,
    stack: bool = False,
) -> timeseries.Panel:
    """A time series panel with optional fixed legend colors and stacking.

    Ratio panels keep a fixed 0 to 1 axis so the curve does not rescale as values change.
    """
    panel = (
        timeseries.Panel()
        .title(title)
        .description(description)
        .datasource(PROMETHEUS)
        .unit(unit)
        .color_scheme(dashboard.FieldColor().mode(dashboard_models.FieldColorModeId.PALETTE_CLASSIC_BY_NAME))
        .line_width(2)
        .fill_opacity(24 if stack else 8)
        .point_size(8)
        .show_points(models.VisibilityMode.NEVER)
        .legend(
            common.VizLegendOptions()
            .display_mode(models.LegendDisplayMode.TABLE)
            .placement(models.LegendPlacement.BOTTOM)
            .show_legend(True)
            .calcs(["lastNotNull"])
        )
        .tooltip(common.VizTooltipOptions().mode(models.TooltipDisplayMode.MULTI).sort(models.SortOrder.DESCENDING))
        .targets([target.ref_id(chr(ord("A") + index)) for index, target in enumerate(targets)])
        .span(span)
        .height(8)
    )
    if unit == "percentunit":
        panel.min(0).max(1)
    if stack:
        panel.stacking(common.StackingConfig().mode(models.StackingMode.NORMAL).group("A"))
    for name, color in (colors or {}).items():
        panel.override_by_name(
            name,
            [dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": color})],
        )
    return panel


def latency(
    bucket_rates: str,
    dimensions: str,
    title: str,
    description: str,
    *,
    unit: str,
    scale: int = 1,
    mean_rates: tuple[str, str] | None = None,
    span: int = 8,
) -> timeseries.Panel:
    """A raw-histogram latency panel with fixed units and optional interval-local mean."""
    targets = []
    for name, quantile in (("p50", 0.50), ("p90", 0.90), ("p99", 0.99)):
        expr = f"max({histogram_quantile(bucket_rates, dimensions, quantile)})"
        if scale != 1:
            expr = f"{scale} * {expr}"
        targets.append(foretoken_query(expr, name))
    colors = QUANTILE_COLORS
    if mean_rates is not None:
        sum_rates, count_rates = mean_rates
        mean_expr = (
            f"max(sum by({dimensions}) ({sum_rates}) / "
            f"clamp_min(sum by({dimensions}) ({count_rates}), 1e-9))"
        )
        if scale != 1:
            mean_expr = f"{scale} * {mean_expr}"
        targets.append(foretoken_query(mean_expr, "mean"))
        colors = {**QUANTILE_COLORS, "mean": ORANGE}
    return series(title, description, targets, unit=unit, span=span, colors=colors).decimals(2)


def distribution(title: str, description: str, metric: str) -> heatmap.Panel:
    """A heatmap of a raw request-length histogram using the selected rate window."""
    return (
        heatmap.Panel()
        .title(title)
        .description(description)
        .datasource(PROMETHEUS)
        .with_target(
            prometheus.Dataquery()
            .datasource(PROMETHEUS)
            .expr(f"sum by(le) ({model_metric(metric, rate=True)})")
            .format(prometheus_models.PromQueryFormat.HEATMAP)
            .legend_format("{{le}}")
            .interval("5s")
            .range()
            .ref_id("A")
        )
        .calculate(False)
        .cell_gap(1)
        .color(heatmap.HeatmapColorOptions().mode(heatmap_models.HeatmapColorMode.SCHEME).scheme("Blues").steps(64))
        .y_axis(heatmap.YAxisConfig().unit("short"))
        .span(12)
        .height(8)
    )


def by_device(title: str, description: str, rule: str, *, unit: str) -> timeseries.Panel:
    """Build a per-device GPU panel for the shared system dashboard."""
    return series(
        title,
        description,
        [query(f"max by(node, device_id) ({rule})", DEVICE_LEGEND)],
        unit=unit,
        span=6,
    )


def autoscaling(
    title: str, description: str, metrics: dict[str, str], *, unit: str, age: bool = False
) -> timeseries.Panel:
    """One line per published autoscaling target for each metric (legend suffix -> metric name).

    With `age`, the metrics are Unix timestamps and the panel shows how long ago they were set.
    """
    prefix = "time() - " if age else ""
    targets = [
        query(f"{prefix}max by({AUTOSCALING_TARGET}) ({metric}{{{SERVICE}}})", f"{suffix} / {AUTOSCALING_LEGEND}")
        for suffix, metric in metrics.items()
    ]
    return series(title, description, targets, unit=unit, span=8)


def variable(name: str, label: str, query_text: str) -> dashboard.QueryVariable:
    return (
        dashboard.QueryVariable(name)
        .label(label)
        .datasource(PROMETHEUS)
        .query(query_text)
        .refresh(dashboard_models.VariableRefresh.ON_DASHBOARD_LOAD)
        .sort(dashboard_models.VariableSort.ALPHABETICAL_ASC)
        .include_all(True)
        .all_value(".*")
        .multi(True)
    )


def localize_dashboard(value: object) -> object:
    """Translate visible dashboard strings while leaving queries and metric identities unchanged."""
    visible_keys = {"title", "description", "label", "legendFormat", "noValue"}
    if isinstance(value, dict):
        localized = {
            key: ZH.get(item, item) if key in visible_keys and isinstance(item, str) else localize_dashboard(item)
            for key, item in value.items()
        }
        if value.get("id") == "byName" and isinstance(value.get("options"), str):
            localized["options"] = ZH.get(value["options"], value["options"])
        if value.get("id") == "organize" and isinstance(localized.get("options"), dict):
            localized["options"]["renameByName"] = AUTOSCALING_COLUMNS_ZH
        return localized
    if isinstance(value, list):
        return [localize_dashboard(item) for item in value]
    return value


def render(locale: str) -> str:
    """Render one locale from the shared dashboard model and PromQL definitions."""
    encoded = JSONEncoder(sort_keys=True, indent=2).encode(build())
    if locale == "en":
        return encoded
    payload = localize_dashboard(json.loads(encoded))
    payload["uid"] = "foretoken-system-overview-zh"
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def build() -> dashboard_models.Dashboard:
    board = (
        dashboard.Dashboard("Foretoken System Overview")
        .uid("foretoken-system-overview")
        .tags(["foretoken", "inference", "operations"])
        .editable()
        .tooltip(dashboard_models.DashboardCursorSync.CROSSHAIR)
        .refresh("5s")
        .time("now-30m", "now")
        .timezone("browser")
        .links([])
        .annotation(
            dashboard.AnnotationQuery()
            .name("Annotations & Alerts")
            .datasource(dashboard_models.DataSourceRef(type_val="grafana", uid="-- Grafana --"))
            .built_in(1)
            .enable(True)
            .hide(True)
            .icon_color("rgba(0, 211, 255, 1)")
        )
        .with_variable(dashboard.DatasourceVariable("DS_PROMETHEUS").label("Data source").type("prometheus"))
        .with_variable(variable("namespace", "Namespace", "label_values(foretoken:frontend_up:sum, namespace)"))
        .with_variable(
            variable(
                "frontend_service",
                "Frontend service",
                'label_values(foretoken:frontend_up:sum{namespace=~"$namespace"}, frontend_service)',
            )
        )
        .with_variable(
            variable(
                "model_group",
                "Model group",
                'label_values(foretoken:model_server_up:sum{namespace=~"$namespace"}, model_group)',
            )
        )
        .with_variable(
            variable(
                "model_role",
                "Model role",
                'label_values(foretoken:model_server_up:sum{namespace=~"$namespace",model_group=~"$model_group"}, model_role)',
            )
        )
        .with_variable(
            variable(
                "model_name",
                "Model",
                f"label_values(foretoken:model_server_kv_cache_usage_ratio:max{{{GROUP}}}, model_name)",
            )
        )
        .with_variable(
            variable(
                "model_service",
                "Model service",
                'label_values(foretoken_autoscaling_applied_replicas{namespace=~"$namespace"}, modelservice)',
            )
        )
    )

    frontend_request_rates = frontend_metric("http_requests_total", rate=True)
    frontend_5xx_rates = frontend_metric("http_requests_total", extra='status="5xx"', rate=True)

    board.with_row(dashboard.Row("Overview"))
    board.with_panel(
        headline(
            "Frontend targets",
            "Prometheus targets currently reporting for the selected Frontend services.",
            f"sum(foretoken:frontend_up:sum{{{FRONTEND}}})",
            color=None,
            thresholds=steps((None, RED), (1, GREEN)),
        )
    )
    board.with_panel(
        headline(
            "Model servers",
            "Prometheus targets currently reporting for the selected model groups and roles.",
            f"sum(foretoken:model_server_up:sum{{{GROUP}}})",
            color=None,
            thresholds=steps((None, RED), (1, GREEN)),
        )
    )
    board.with_panel(
        headline(
            "Requests / s",
            "Frontend responses started per second over the selected rate window.",
            f"sum({frontend_request_rates})",
            unit="reqps",
            interval="5s",
        )
    )
    board.with_panel(
        headline(
            "5xx ratio",
            "HTTP responses that started with 5xx divided by all started responses. "
            "Streaming failures after headers are not included.",
            f"(sum({frontend_5xx_rates}) or 0 * sum({frontend_request_rates})) "
            f"/ clamp_min(sum({frontend_request_rates}), 1e-9)",
            unit="percentunit",
            color=None,
            thresholds=steps((None, GREEN), (0.01, AMBER), (0.05, RED)),
            interval="5s",
        )
    )
    board.with_panel(
        headline(
            "Prompt tokens / s",
            "Prompt tokens processed per second by the selected model servers.",
            f"sum({model_metric('vllm:prompt_tokens_total', rate=True)})",
            unit="suffix: tok/s",
            interval="5s",
        )
    )
    board.with_panel(
        headline(
            "Output tokens / s",
            "Generated tokens produced per second by the selected model servers.",
            f"sum({model_metric('vllm:generation_tokens_total', rate=True)})",
            unit="suffix: tok/s",
            color=ORANGE,
            interval="5s",
        )
    )
    board.with_panel(
        headline(
            "Queued requests",
            "Requests waiting for frontend admission to a scaling target.",
            f"sum(foretoken:frontend_upstream_queued_requests:sum{{{FRONTEND}}})",
            color=None,
            thresholds=steps((None, GREEN), (1, ORANGE)),
        )
    )
    board.with_panel(
        headline(
            "Temporary RuntimeCache",
            "One when any selected model-server has fallen back to Pod-scoped temporary cache storage. "
            "No value means the selected groups do not use RuntimeCache.",
            f"max(foretoken:model_server_runtime_cache_temporary:max{{{GROUP}}})",
            color=None,
            thresholds=steps((None, GREEN), (1, RED)),
            no_value="Not configured",
        )
    )

    board.with_row(dashboard.Row("Frontend"))
    board.with_panel(
        series(
            "Request rate by status",
            "Frontend response starts grouped by HTTP status class.",
            [foretoken_query(f"sum by(status) ({frontend_request_rates})", "{{status}}")],
            unit="reqps",
            span=6,
            colors={"2xx": GREEN, "4xx": ORANGE, "5xx": RED},
            stack=True,
        )
    )
    board.with_panel(
        series(
            "Request rate by endpoint",
            "Frontend response starts grouped by HTTP endpoint.",
            [foretoken_query(f"sum by(handler) ({frontend_request_rates})", "{{handler}}")],
            unit="reqps",
            span=6,
            stack=True,
        )
    )
    board.with_panel(
        latency(
            frontend_metric(
                "http_request_duration_seconds_bucket",
                extra='handler=~"/v1/(chat/completions|completions|generate)"',
                rate=True,
            ),
            f"{FRONTEND_DIMENSIONS},handler",
            "Response-start latency",
            "Time, in seconds, until the Frontend handler produces HTTP response headers. "
            "This excludes SSE body delivery; it is neither TTFT nor full-stream duration.",
            unit="suffix: s",
            span=6,
        )
    )
    board.with_panel(
        series(
            "Admission queue",
            "Requests waiting for runtime preparation or backend dispatch, grouped by scaling-target kind.",
            [
                query(
                    f"sum by(target_kind) (foretoken:frontend_upstream_queued_requests:sum{{{FRONTEND}}})",
                    "{{target_kind}}",
                )
            ],
            unit="short",
            span=6,
            colors={"Pool": BLUE, "EPDPipelineScope": ORANGE},
        )
    )

    board.with_row(dashboard.Row("Model Serving"))
    board.with_panel(
        series(
            "Completed request rate",
            "Successfully completed model-server requests grouped by finish reason.",
            [
                foretoken_query(
                    f"sum by(finished_reason) ({model_metric('vllm:request_success_total', rate=True)})",
                    "{{finished_reason}}",
                )
            ],
            unit="reqps",
            span=8,
            stack=True,
        )
    )
    board.with_panel(
        series(
            "Token throughput",
            "Prompt and generated token throughput for the selected model servers.",
            [
                foretoken_query(f"sum({model_metric('vllm:prompt_tokens_total', rate=True)})", "Prompt"),
                foretoken_query(f"sum({model_metric('vllm:generation_tokens_total', rate=True)})", "Output"),
            ],
            unit="suffix: tok/s",
            span=8,
            colors={"Prompt": BLUE, "Output": ORANGE},
        )
    )
    board.with_panel(
        series(
            "Scheduler state",
            "Requests running in vLLM execution batches or waiting in its scheduler.",
            [
                foretoken_query(f"sum({model_metric('vllm:num_requests_running')})", "Running"),
                foretoken_query(f"sum({model_metric('vllm:num_requests_waiting')})", "Waiting"),
            ],
            unit="short",
            span=8,
            colors={"Running": BLUE, "Waiting": ORANGE},
        )
    )
    board.with_panel(
        latency(
            model_metric("vllm:e2e_request_latency_seconds_bucket", rate=True),
            MODEL_DIMENSIONS,
            "End-to-end latency (E2EL)",
            "Maximum per-model-group quantile, in seconds, from Frontend handler entry after JSON decoding "
            "to the model-server terminal output. Excludes downstream client body consumption. "
            "Frontend and model-server clocks must be synchronized.",
            unit="suffix: s",
        )
    )
    board.with_panel(
        latency(
            model_metric("vllm:time_to_first_token_seconds_bucket", rate=True),
            MODEL_DIMENSIONS,
            "Time to first token (TTFT)",
            "Maximum per-model-group TTFT quantile, in seconds, from Frontend handler entry after JSON "
            "decoding to the first token received by model-server. Chat and completion requests share this origin.",
            unit="suffix: s",
        )
    )
    board.with_panel(
        latency(
            model_metric("vllm:request_time_per_output_token_seconds_bucket", rate=True),
            MODEL_DIMENSIONS,
            "Time per output token (TPOT)",
            "Maximum per-model-group request-level TPOT quantiles and mean over the selected rate window, "
            "in milliseconds. Each request contributes its average time between output tokens.",
            unit="suffix: ms",
            scale=1_000,
            mean_rates=(
                model_metric("vllm:request_time_per_output_token_seconds_sum", rate=True),
                model_metric("vllm:request_time_per_output_token_seconds_count", rate=True),
            ),
        )
    )
    board.with_panel(
        latency(
            model_metric("vllm:inter_token_latency_seconds_bucket", rate=True),
            MODEL_DIMENSIONS,
            "Inter-token latency (ITL)",
            "Maximum per-model-group gap between consecutive output-token events, in milliseconds. "
            "Unlike TPOT, each token interval is observed separately.",
            unit="suffix: ms",
            scale=1_000,
        )
    )
    board.with_panel(
        series(
            "Request time by stage",
            "P90 time, in seconds, a request spends waiting for the scheduler, in prefill, and in decode.",
            [
                foretoken_query(
                    f"max({histogram_quantile(model_metric(metric, rate=True), MODEL_DIMENSIONS, 0.90)})",
                    stage,
                )
                for stage, metric in (
                    ("queue", "vllm:request_queue_time_seconds_bucket"),
                    ("prefill", "vllm:request_prefill_time_seconds_bucket"),
                    ("decode", "vllm:request_decode_time_seconds_bucket"),
                )
            ],
            unit="suffix: s",
            span=8,
            colors=STAGE_COLORS,
        )
    )
    board.with_panel(
        series(
            "Preemptions",
            "Requests preempted per second because KV-cache blocks ran out. Sustained preemption "
            "precedes the KV-cache pressure alert.",
            [foretoken_query(f"sum({model_metric('vllm:num_preemptions_total', rate=True)})", "Preemptions")],
            unit="ops",
            span=8,
            colors={"Preemptions": RED},
        )
    )
    board.with_panel(
        distribution(
            "Prompt length",
            "Distribution of prompt tokens per request over time.",
            "vllm:request_prompt_tokens_bucket",
        )
    )
    board.with_panel(
        distribution(
            "Output length",
            "Distribution of generated tokens per request over time.",
            "vllm:request_generation_tokens_bucket",
        )
    )

    board.with_row(dashboard.Row("Cache"))
    board.with_panel(
        series(
            "KV Cache utilization",
            "Highest in-engine KV-cache utilization grouped by model role.",
            [query(f"max by(model_role) (foretoken:model_server_kv_cache_usage_ratio:max{{{MODEL}}})", "{{model_role}}")],
            unit="percentunit",
            span=8,
        )
    )
    board.with_panel(
        series(
            "Prefix Cache hit ratio",
            "Average local and external Prefix Cache token hit ratios across selected model groups.",
            [
                foretoken_query(
                    model_rate_ratio("vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total"),
                    "local",
                ),
                foretoken_query(
                    model_rate_ratio(
                        "vllm:external_prefix_cache_hits_total",
                        "vllm:external_prefix_cache_queries_total",
                    ),
                    "external",
                ),
            ],
            unit="percentunit",
            span=8,
            colors={"local": BLUE, "external": TEAL},
        )
    )
    board.with_panel(
        series(
            "KV index source health",
            "Healthy KV event sources divided by configured sources. Disabled or unavailable indexing reports zero.",
            [query(f"foretoken:frontend_kv_index_source_health_ratio:min{{{FRONTEND}}}", "{{frontend_service}}")],
            unit="percentunit",
            span=8,
        )
    )
    board.with_panel(
        series(
            "RuntimeCache utilization",
            "Mounted RuntimeCache filesystem utilization. Series are absent for model groups without a RuntimeCache.",
            [query(f"foretoken:model_server_runtime_cache_usage_ratio:max{{{GROUP}}}", "{{model_group}} / {{model_role}}")],
            unit="percentunit",
            span=12,
        )
    )
    board.with_panel(
        series(
            "RuntimeCache available space",
            "Filesystem space available to model-server processes using a RuntimeCache.",
            [
                query(
                    f"foretoken:model_server_runtime_cache_available_bytes:min{{{GROUP}}}",
                    "{{model_group}} / {{model_role}}",
                )
            ],
            unit="bytes",
            span=12,
        )
    )

    board.with_row(dashboard.Row("Accelerators and Resources"))
    gpu_note = (
        "by vendor and node across workload namespaces. Shared devices are counted once; "
        "devices without Foretoken Pod attribution are excluded."
    )
    board.with_panel(
        series(
            "Node mean GPU utilization",
            "Mean utilization of Foretoken-attributed GPUs " + gpu_note,
            [
                query(
                    "avg by(vendor, node) (max by(vendor, node, device_id) (foretoken:accelerator_gpu_utilization_ratio))",
                    "{{vendor}} / {{node}}",
                )
            ],
            unit="percentunit",
            span=6,
        )
    )
    board.with_panel(
        series(
            "Node mean GPU memory utilization",
            "Mean memory utilization of Foretoken-attributed GPUs " + gpu_note,
            [
                query(
                    "avg by(vendor, node) (max by(vendor, node, device_id) (foretoken:accelerator_gpu_memory_usage_ratio))",
                    "{{vendor}} / {{node}}",
                )
            ],
            unit="percentunit",
            span=6,
        )
    )
    board.with_panel(
        by_device(
            "GPU utilization by device",
            "Utilization of each Foretoken-attributed GPU.",
            "foretoken:accelerator_gpu_utilization_ratio",
            unit="percentunit",
        )
    )
    board.with_panel(
        by_device(
            "GPU memory by device",
            "Memory utilization of each Foretoken-attributed GPU.",
            "foretoken:accelerator_gpu_memory_usage_ratio",
            unit="percentunit",
        )
    )
    board.with_panel(
        by_device(
            "GPU power by device",
            "Power draw of each Foretoken-attributed NVIDIA GPU.",
            "foretoken:accelerator_gpu_power_watts",
            unit="watt",
        )
    )
    board.with_panel(
        by_device(
            "GPU temperature by device",
            "Temperature of each Foretoken-attributed NVIDIA GPU.",
            "foretoken:accelerator_gpu_temperature_celsius",
            unit="celsius",
        )
    )
    container = 'namespace=~"$namespace",container=~"frontend|model-server"'
    board.with_panel(
        series(
            "Serving CPU usage",
            "CPU cores consumed by Frontend and model-server containers in the selected namespaces.",
            [
                query(
                    f"sum by(container) (rate(container_cpu_usage_seconds_total{{{container}}}[$__rate_interval]))",
                    "{{container}}",
                    interval="30s",
                )
            ],
            unit="cores",
            span=6,
            colors={"frontend": BLUE, "model-server": ORANGE},
        )
    )
    board.with_panel(
        series(
            "Serving memory usage",
            "Working-set memory used by Frontend and model-server containers in the selected namespaces.",
            [
                query(
                    f"sum by(container) (container_memory_working_set_bytes{{{container}}})",
                    "{{container}}",
                    interval="30s",
                )
            ],
            unit="bytes",
            span=6,
            colors={"frontend": BLUE, "model-server": ORANGE},
        )
    )

    # Routing and control-plane sections are collapsed: they matter when investigating the
    # platform rather than during routine checks of the request path.
    routing = dashboard.Row("Routing decisions")
    routing.with_panel(
        series(
            "Routing outcomes",
            "Selection results by workflow round. A selection failure is not an HTTP status; "
            "a request can involve several rounds.",
            [
                foretoken_query(
                    f"sum by(round,outcome) (rate(foretoken_router_selections_total{{{ROUTER}}}[$__rate_interval]))",
                    "{{round}} / {{outcome}}",
                )
            ],
            unit="reqps",
            span=8,
        )
    )
    routing.with_panel(
        series(
            "Routing stage latency",
            "P99 filter, scorer and picker execution time, aggregated from histogram buckets "
            "across selected Frontend replicas.",
            [
                foretoken_query(
                    "histogram_quantile(0.99, sum by(round,stage,algorithm,le) "
                    f"(rate(foretoken_router_stage_duration_seconds_bucket{{{ROUTER}}}[$__rate_interval])))",
                    "{{round}} / {{stage}} / {{algorithm}}",
                )
            ],
            unit="s",
            span=8,
        )
    )
    routing.with_panel(
        series(
            "Routing candidates",
            "Mean available, filtered and selectable candidate counts. Selectable counts include data-parallel ranks.",
            [
                foretoken_query(
                    f"sum by(round,stage) (rate(foretoken_router_candidates_sum{{{ROUTER}}}[$__rate_interval])) "
                    f"/ sum by(round,stage) (rate(foretoken_router_candidates_count{{{ROUTER}}}[$__rate_interval]))",
                    "{{round}} / {{stage}}",
                )
            ],
            unit="short",
            span=8,
        )
    )
    board.with_row(routing)

    control_plane = dashboard.Row("Control plane")
    control_plane.with_panel(
        series(
            "Reconcile errors",
            "Controller-runtime errors per second. This section observes the platform controller "
            "independently of workload namespace filters.",
            [
                foretoken_query(
                    f"sum by(controller) (rate(controller_runtime_reconcile_errors_total{{{CONTROLLER}}}[$__rate_interval]))",
                    "{{controller}}",
                )
            ],
            unit="ops",
            span=8,
        )
    )
    control_plane.with_panel(
        series(
            "Reconcile latency",
            "P99 reconciliation time by controller; this is control-plane work, not inference request latency.",
            [
                foretoken_query(
                    "histogram_quantile(0.99,sum by(controller,le) "
                    f"(rate(controller_runtime_reconcile_time_seconds_bucket{{{CONTROLLER}}}[$__rate_interval])))",
                    "{{controller}}",
                )
            ],
            unit="s",
            span=8,
        )
    )
    control_plane.with_panel(
        series(
            "Controller workqueues",
            "Queued reconciliations. Current replica counts are deduplicated rather than added "
            "across controller replicas.",
            [foretoken_query(f"max by(name) (workqueue_depth{{{CONTROLLER}}})", "{{name}}")],
            unit="short",
            span=8,
        )
    )
    board.with_row(control_plane)

    board.with_row(dashboard.Row("Autoscaling decisions"))
    board.with_panel(
        autoscaling(
            "Replica decisions",
            "Latest published recommendation, adjusted target and applied desired capacity. "
            "A missing recommendation means the decision algorithm did not provide one.",
            {
                "recommendation": "foretoken_autoscaling_recommendation_replicas",
                "adjusted": "foretoken_autoscaling_adjusted_replicas",
                "applied": "foretoken_autoscaling_applied_replicas",
            },
            unit="short",
        )
    )
    board.with_panel(
        autoscaling(
            "Serving capacity",
            "Ready and routable capacity published by the autoscaler, compared with applied desired replicas.",
            {
                "applied": "foretoken_autoscaling_applied_replicas",
                "ready": "foretoken_autoscaling_ready_replicas",
                "routable": "foretoken_autoscaling_routable_replicas",
            },
            unit="short",
        )
    )
    board.with_panel(
        autoscaling(
            "Observation and evaluation age",
            "Elapsed time since the last observation and evaluation; age keeps increasing if reconciliation "
            "stops. Missing observation series means no usable observation was published.",
            {
                "observation": "foretoken_autoscaling_observation_timestamp_seconds",
                "evaluation": "foretoken_autoscaling_evaluation_timestamp_seconds",
            },
            unit="s",
            age=True,
        )
    )
    stage_columns = ["namespace", "modelservice", "target_kind", "target_name", "role", "stage", "algorithm", "disposition", "reason"]
    board.with_panel(
        table.Panel()
        .title("Latest autoscaling stage")
        .description(
            "Current trigger, decision and adjustment outcomes from ModelService status. "
            "Reasons are bounded status codes, not log messages."
        )
        .datasource(PROMETHEUS)
        .show_header(True)
        .cell_height(models.TableCellHeight.SM)
        .with_target(
            prometheus.Dataquery()
            .datasource(PROMETHEUS)
            .expr(f"max by({','.join(stage_columns)}) (foretoken_autoscaling_stage{{{SERVICE}}})")
            .format(prometheus_models.PromQueryFormat.TABLE)
            .interval("5s")
            .instant()
            .ref_id("A")
        )
        # The instant query returns Time and Value columns that carry no information here.
        .with_transformation(
            dashboard_models.DataTransformerConfig(
                id_val="organize",
                options={
                    "excludeByName": {"Time": True, "Value": True},
                    "indexByName": {column: index for index, column in enumerate(stage_columns)},
                },
            )
        )
        .span(24)
        .height(9)
    )
    return board.build()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a localized Foretoken Grafana dashboard.")
    parser.add_argument("--locale", choices=("en", "zh"), default="en")
    print(render(parser.parse_args().locale))

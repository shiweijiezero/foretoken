# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Builds the Foretoken System Overview dashboard shipped by the Helm chart.

Run `make dashboard` from the repository root after changing this file. It writes the English
and Chinese JSON dashboards under `deploy/charts/foretoken/files/grafana/`; the chart installs
both through one Grafana ConfigMap. The generated JSON files are deployed artifacts, and this
module is their shared source.

The dashboard reads raw Frontend and model-server metrics over a selected rate interval,
plus recording rules and bounded router, controller, and autoscaling metrics. Model-serving health,
latency, caches, resources, and routing come first; shared frontend and platform diagnostics follow.
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
QUANTILE_COLORS = {"p50": "#86b6ef", "p95": "#3987e5", "p99": "#184f95"}
STAGE_COLORS = {"queue": AMBER, "prefill": BLUE, "decode": TEAL}

# Exact UI-string translations keep both dashboards on one query and layout definition.
ZH = {
    "Foretoken System Overview": "Foretoken 系统概览",
    "Overview": "概览",
    "Shared frontend": "共享前端",
    "Model Serving": "模型服务",
    "Cache": "缓存",
    "Accelerators and Resources": "加速器与资源",
    "NVIDIA power and temperature": "NVIDIA 功耗与温度",
    "Routing decisions": "路由决策",
    "Control plane": "控制面",
    "Autoscaling decisions": "扩缩容决策",
    "Frontend scrape targets": "前端监控端点数",
    "Model scrape targets": "模型监控端点数",
    "Frontend response starts / s": "前端响应开始速率",
    "Frontend HTTP 5xx ratio": "前端 HTTP 5xx 比例",
    "Prompt tokens / s": "输入吞吐量（TPS）",
    "Output tokens / s": "输出吞吐量（TPS）",
    "Frontend queued requests": "前端排队请求",
    "Frontend responses by HTTP status": "前端 HTTP 响应状态",
    "Frontend responses by endpoint": "前端各端点响应速率",
    "Frontend response-header latency": "前端响应头延迟",
    "Frontend admission queue": "前端准入队列",
    "Completed request rate": "完成请求速率",
    "Token throughput": "Token 吞吐量",
    "Scheduler state": "调度器状态",
    "End-to-end latency (E2EL)": "端到端延迟 (E2EL)",
    "Time to first token (TTFT)": "首 token 延迟 (TTFT)",
    "Time per output token (TPOT)": "每输出 token 耗时 (TPOT)",
    "Inter-token latency (ITL)": "Token 间延迟 (ITL)",
    "Request time by stage": "各阶段请求耗时",
    "Preemptions": "抢占",
    "Prompt length": "输入长度",
    "Output length": "输出长度",
    "KV Cache utilization": "KV Cache 使用率",
    "Prefix Cache hit ratio": "Prefix Cache 命中率",
    "Frontend cache-index health": "前端缓存索引健康度",
    "Storage usage": "存储使用率",
    "Available storage": "存储可用空间",
    "GPU utilization by device": "各设备 GPU 使用率",
    "GPU memory by device": "各设备 GPU 显存使用率",
    "NVIDIA GPU power by device": "NVIDIA GPU 功耗",
    "NVIDIA GPU temperature by device": "NVIDIA GPU 温度",
    "Serving CPU usage": "服务 CPU 使用量",
    "Serving memory usage": "服务内存使用量",
    "Routing outcomes": "路由结果",
    "Routing stage latency": "路由阶段延迟",
    "Eligible instances and ranks": "路由候选数量（请求平均）",
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
    "Model instance": "模型实例",
    "Execution role": "执行角色",
    "Model": "模型",
    "Autoscaling service": "扩缩容服务",
    "Output": "输出",
    "Running": "运行中",
    "Waiting": "等待中",
    "mean": "平均值",
    "recommendation / {{modelservice}} / {{target_name}} / {{role}}":
        "建议 / {{modelservice}} / {{target_name}} / {{role}}",
    "adjusted / {{modelservice}} / {{target_name}} / {{role}}": "调整后 / {{modelservice}} / {{target_name}} / {{role}}",
    "applied / {{modelservice}} / {{target_name}} / {{role}}": "已应用 / {{modelservice}} / {{target_name}} / {{role}}",
    "ready / {{modelservice}} / {{target_name}} / {{role}}": "就绪 / {{modelservice}} / {{target_name}} / {{role}}",
    "routable / {{modelservice}} / {{target_name}} / {{role}}": "可路由 / {{modelservice}} / {{target_name}} / {{role}}",
    "observation / {{modelservice}} / {{target_name}} / {{role}}": "观测 / {{modelservice}} / {{target_name}} / {{role}}",
    "evaluation / {{modelservice}} / {{target_name}} / {{role}}": "评估 / {{modelservice}} / {{target_name}} / {{role}}",
    "Prometheus targets currently reporting for the selected Frontend services.":
        "所选前端服务中，最近一次指标抓取成功的端点数量。",
    "Prometheus targets currently reporting for the selected model groups and roles.":
        "所选模型组中，最近一次指标抓取成功的端点数量。",
    "Frontend responses started per second over the selected rate window.": "选定速率窗口内每秒开始的 Frontend 响应数。",
    "HTTP responses that started with 5xx divided by all started responses. Streaming failures after headers are not included.":
        "开始时状态为 5xx 的 HTTP 响应占全部已开始响应的比例，不包含响应头发出后的流式失败。",
    "Prompt tokens processed per second by the selected model servers.": "所选模型服务每秒处理的输入 token 数。",
    "Generated tokens produced per second by the selected model servers.": "所选模型服务器每秒生成的输出 token 数。",
    "Requests waiting for frontend admission to a scaling target.": "正在等待 Frontend 准入到扩缩容目标的请求数。",
    "Frontend response starts grouped by HTTP status class.": "按 HTTP 状态类别分组的 Frontend 响应开始速率。",
    "Frontend response starts grouped by HTTP endpoint.": "按 HTTP 端点分组的 Frontend 响应开始速率。",
    "Time, in seconds, until the Frontend handler produces HTTP response headers. This excludes SSE body delivery; it is neither TTFT nor full-stream duration.":
        "Frontend handler 生成 HTTP 响应头所需的秒数，不包含 SSE 正文传输，也不等同于 TTFT 或完整流持续时间。",
    "Requests waiting for runtime preparation or backend dispatch, grouped by scaling-target kind.":
        "按扩缩容目标类型分组，等待运行时准备或后端派发的请求数。",
    "Completed execution requests by backend and finish reason. A backend is one model instance and data-parallel rank; multi-stage requests can complete once in each role.":
        "按后端和结束原因展示已完成请求。一个后端对应一个模型实例和数据并行 rank；分离式请求会分别在各角色完成一次执行。",
    "Prompt and generated tokens per second by backend. A backend is one model instance and data-parallel rank.":
        "按后端展示输入与输出 token 吞吐量。一个后端对应一个模型实例和数据并行 rank。",
    "Requests running in vLLM execution batches or waiting in its scheduler.": "正在 vLLM 执行批次中运行或在调度器中等待的请求数。",
    "Request-weighted latency from frontend processing to generation completion across the selected engines. Excludes downstream client delivery; clocks must be synchronized.":
        "将所选引擎的请求样本合并后统计：从前端处理开始到生成完成的耗时，不含下游客户端接收时间；节点时钟须同步。",
    "Request-weighted time from frontend processing to the first token at model-server across the selected engines. Excludes downstream client delivery.":
        "将所选引擎的请求样本合并后统计：从前端处理开始到模型服务器收到首个 token 的耗时，不含下游客户端接收时间。",
    "Request-weighted TPOT across selected engines, in milliseconds. Each request contributes its average output-token interval.":
        "所选引擎合并后的请求级 TPOT，单位毫秒；每个请求贡献一次平均输出 token 间隔。",
    "Output-token interval distribution across selected engines, in milliseconds. Each token interval is one observation.":
        "所选引擎的输出 token 间隔分布，单位毫秒；每个间隔贡献一次观测。",
    "P95 time, in seconds, a request spends waiting for the scheduler, in prefill, and in decode.":
        "请求在等待调度器、Prefill 和 Decode 阶段的 P95 耗时，单位为秒。",
    "Scheduler preemptions per second by backend; sustained activity indicates KV-cache pressure.":
        "按后端统计每秒调度抢占次数；持续抢占表示 KV 缓存压力。",
    "Distribution of prompt tokens per request over time.": "各时间段内请求输入 token 数的分布。",
    "Distribution of generated tokens per request over time.": "随时间变化的单请求输出 token 数分布。",
    "KV-cache occupancy by model instance and engine rank.": "按模型实例和引擎 rank 展示 KV 缓存占用率。",
    "Cache hits divided by queried tokens across selected engines. Idle or unavailable caches have no ratio; local and external observations are separate.":
        "所选引擎的命中 token 数除以查询 token 数。没有查询或指标不可用时不显示比例；本地与外部缓存分别统计。",
    "Healthy KV event sources divided by configured sources. Disabled or unavailable indexing reports zero.":
        "健康 KV 事件源数除以已配置源数；索引禁用或不可用时为 0。",
    "Mounted RuntimeCache filesystem utilization. Series are absent for model groups without a RuntimeCache.":
        "已挂载 RuntimeCache 文件系统的使用率；未配置 RuntimeCache 的模型组不会产生序列。",
    "Filesystem space available to model-server processes using a RuntimeCache.": "使用 RuntimeCache 的模型服务器进程可用文件系统空间。",
    "Utilization of each Foretoken-attributed GPU.": "每块可关联到 Foretoken 的 GPU 使用率。",
    "Memory utilization of each Foretoken-attributed GPU.": "每块可关联到 Foretoken 的 GPU 显存使用率。",
    "Power draw of each Foretoken-attributed NVIDIA GPU.": "每块可关联到 Foretoken 的 NVIDIA GPU 功耗。",
    "Temperature of each Foretoken-attributed NVIDIA GPU.": "每块可关联到 Foretoken 的 NVIDIA GPU 温度。",
    "CPU cores used by model-server Pods belonging to the selected model instances.": "所选模型实例的模型服务器 Pod 使用的 CPU 核数。",
    "Working-set memory of model-server Pods belonging to the selected model instances.": "所选模型实例的模型服务器 Pod 工作集内存。",
    "Routing results observed within the selected time range, grouped by selection round.":
        "仅展示所选时间范围内发生过的路由结果，按选择阶段分组。",
    "P99 filter, scorer and picker execution time, aggregated from histogram buckets across selected Frontend replicas.":
        "从所选 Frontend 副本直方图桶聚合得到的 Filter、Scorer 与 Picker P99 执行时间。",
    "Mean available, filtered and selectable candidate counts. Selectable counts include data-parallel ranks.":
        "可用、过滤后和可选择候选项的平均数量；可选择数量包含数据并行 rank。",
    "Controller-runtime errors per second. This section observes the platform controller independently of workload namespace filters.":
        "每秒 controller-runtime 错误数；本分区独立于工作负载命名空间筛选器观察平台控制器。",
    "P99 reconciliation time by controller; this is control-plane work, not inference request latency.":
        "按控制器统计的 P99 Reconcile 耗时，属于控制面工作而非推理请求延迟。",
    "Queued reconciliations, deduplicated across controller replicas.": "待处理的协调任务数，跨控制器副本去重。",
    "Latest published recommendation, adjusted target and applied desired capacity. A missing recommendation means the decision algorithm did not provide one.":
        "最新发布的建议值、调整后目标和已应用期望容量；建议值缺失表示决策算法未提供建议。",
    "Ready and routable capacity published by the autoscaler, compared with applied desired replicas.":
        "扩缩容器发布的 Ready 与可路由容量，并与已应用的期望副本数比较。",
    "Elapsed time since the last observation and evaluation; age keeps increasing if reconciliation stops. Missing observation series means no usable observation was published.":
        "距最近一次观测和评估的时间；Reconcile 停止后该时长会继续增加。观测序列缺失表示尚未发布可用观测。",
    "Current trigger, decision and adjustment outcomes from ModelService status. Reasons are bounded status codes, not log messages.":
        "ModelService status 中当前的触发、决策与调整结果；原因是有限状态码，而不是日志文本。",
    "No data": "无数据",
    "Engine rank": "引擎 rank",
    "Routing share by backend": "各后端路由占比",
    "Share of routing selections within each model and execution role. Each backend is one model instance and data-parallel rank; all backends form the denominator. This counts choices, not completed requests.":
        "每个模型及执行角色内，各后端获得的路由选择比例。一个后端对应一个模型实例和数据并行 rank，分母为该模型该角色的全部后端；表示选择次数，不是完成请求数。",
    "Scheduler queued requests": "引擎排队请求",
    "Requests waiting in the selected model engines, not the shared frontend admission queue.":
        "所选模型引擎内等待调度的请求，不包含共享前端准入队列。",
    "Output / {{model_name}} / {{model_group_display}} / {{model_role}} / rank {{engine}}":
        "输出 / {{model_name}} / {{model_group_display}} / {{model_role}} / rank {{engine}}",
    "Prompt / {{model_name}} / {{model_group_display}} / {{model_role}} / rank {{engine}}":
        "输入 / {{model_name}} / {{model_group_display}} / {{model_role}} / rank {{engine}}",
    "Running / {{model_group_display}} / {{model_role}} / rank {{engine}}":
        "运行中 / {{model_group_display}} / {{model_role}} / rank {{engine}}",
    "Waiting / {{model_group_display}} / {{model_role}} / rank {{engine}}":
        "等待中 / {{model_group_display}} / {{model_role}} / rank {{engine}}",
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
RAW_FRONTEND = (
    'endpoint="http",namespace=~"$namespace",inference_foretoken_io_frontend_service!="",'
    'inference_foretoken_io_frontend_service=~"$frontend_service"'
)
RAW_MODEL = (
    'endpoint="model-server",namespace=~"$namespace",inference_foretoken_io_model_group!="",'
    'inference_foretoken_io_model_group=~"$model_group",inference_foretoken_io_model_role!="",'
    'inference_foretoken_io_model_role=~"$model_role",model_name=~"$model_name",engine=~"$engine"'
)
ROUTER = (
    'namespace=~"$namespace",inference_foretoken_io_frontend_service=~"$frontend_service",'
    'model_name=~"$model_name"'
)
SERVICE = 'namespace=~"$namespace",modelservice=~"$model_service",model_name=~"$model_name"'
CONTROLLER = 'job="foretoken-control-plane"'
AUTOSCALING_TARGET = "namespace,modelservice,target_kind,target_name,role"
AUTOSCALING_LEGEND = "{{modelservice}} / {{target_name}} / {{role}}"
DEVICE_LEGEND = "{{node}} / {{device_id}}"

def instance_display(expr: str) -> str:
    """Abbreviate long instance names for display while retaining full query identities."""
    return (
        f'label_replace(label_replace({expr}, "model_group_display", "$1", '
        '"model_group", "(.+)"), "model_group_display", "$1…$2", '
        '"model_group", "^(.{20}).{5,}(.{10})$")'
    )


def query(expr: str, legend: str | None = None, *, interval: str | None = None) -> prometheus.Dataquery:
    if legend is not None and "{{model_group_display}}" in legend:
        expr = instance_display(expr)
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


def selected_groups() -> str:
    """Resolve model identity from engine gauges, including idle model instances."""
    return f"max by(namespace,model_group) (0 * ({model_metric('vllm:kv_cache_usage_perc')}) + 1)"


def scoped_group_metric(expr: str) -> str:
    """Restrict group-scoped storage and accelerator observations to the selected model."""
    return f"({expr}) and on(namespace,model_group) ({selected_groups()})"


def routing_target_rates() -> str:
    """Attach readable Group names to selections using the target UID from Service metadata."""
    rates = (
        f'sum by(namespace,model_name,model_role,route_target_id,data_parallel_rank) '
        f'(rate(foretoken_router_target_selections_total{{{ROUTER},model_role=~"$model_role"}}[$__rate_interval]))'
    )
    targets = (
        'max by(namespace,route_target_id,model_group) ('
        'label_replace(label_replace(up{endpoint="model-server",'
        'inference_foretoken_io_model_group_uid!=""}, "route_target_id", "$1", '
        '"inference_foretoken_io_model_group_uid", "(.+)"), "model_group", "$1", '
        '"inference_foretoken_io_model_group", "(.+)"))'
    )
    return f"({rates}) * on(namespace,route_target_id) group_left(model_group) (0 * ({targets}) + 1)"


def model_rate_ratio(numerator_metric: str, denominator_metric: str) -> str:
    """Weight cache reuse by queried tokens; no queries leave the ratio unavailable."""
    numerator = model_metric(numerator_metric, rate=True)
    denominator = model_metric(denominator_metric, rate=True)
    return f"sum by(model_name,model_role) ({numerator}) / (sum by(model_name,model_role) ({denominator}) > 0)"


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
    no_value: str = "No data",
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
        .mappings([
            dashboard_models.SpecialValueMap(
                options=dashboard_models.DashboardSpecialValueMapOptions(
                    match=dashboard_models.SpecialValueMatch.NULL_AND_NAN,
                    result=dashboard_models.ValueMappingResult(text=no_value, color="#808080"),
                )
            )
        ])
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
    title: str,
    description: str,
    *,
    unit: str,
    scale: int = 1,
    mean_rates: tuple[str, str] | None = None,
    span: int = 8,
    dimensions: str = "model_name,model_role",
) -> timeseries.Panel:
    """A raw-histogram latency panel with fixed units and optional interval-local mean."""
    targets = []
    buckets = f"{dimensions},le" if dimensions else "le"
    prefix = "{{model_name}} / {{model_role}} / " if dimensions else ""
    for name, quantile in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
        expr = f"histogram_quantile({quantile}, sum by({buckets}) ({bucket_rates}))"
        if scale != 1:
            expr = f"{scale} * {expr}"
        targets.append(foretoken_query(expr, prefix + name))
    colors = QUANTILE_COLORS
    if mean_rates is not None:
        sum_rates, count_rates = mean_rates
        mean_expr = (
            f"sum by({dimensions}) ({sum_rates}) / (sum by({dimensions}) ({count_rates}) > 0)"
        )
        if scale != 1:
            mean_expr = f"{scale} * {mean_expr}"
        targets.append(foretoken_query(mean_expr, prefix + "mean"))
        colors = {**QUANTILE_COLORS, "mean": ORANGE}
    return series(title, description, targets, unit=unit, span=span, colors=colors if not dimensions else None).decimals(2)


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
        [query(f"max by(vendor,node,device_id) ({scoped_group_metric(f'{rule}{{{GROUP}}}')})", "{{vendor}} / " + DEVICE_LEGEND)],
        unit=unit,
        span=12,
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
    visible_keys = {"title", "description", "label", "legendFormat", "noValue", "text"}
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
    instances = (
        'label_replace(max by(namespace,inference_foretoken_io_model_group) ('
        'vllm:kv_cache_usage_perc{endpoint="model-server",namespace=~"$namespace",'
        'model_name=~"$model_name"}), "model_group", "$1", '
        '"inference_foretoken_io_model_group", "(.+)")'
    )
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
                "model_name", "Model",
                'label_values(vllm:kv_cache_usage_perc{endpoint="model-server",namespace=~"$namespace"}, model_name)',
            )
        )
        .with_variable(
            variable(
                "model_group",
                "Model instance",
                f"query_result({instance_display(instances)})",
            ).regex('/model_group="(?<value>[^"]+)".*model_group_display="(?<text>[^"]+)"/')
        )
        .with_variable(
            variable(
                "model_role",
                "Execution role",
                'label_values(foretoken:model_server_up:sum{namespace=~"$namespace",model_group=~"$model_group"}, model_role)',
            )
        )
        .with_variable(
            variable(
                "engine",
                "Engine rank",
                'label_values(vllm:kv_cache_usage_perc{endpoint="model-server",namespace=~"$namespace",'
                'model_name=~"$model_name",inference_foretoken_io_model_group=~"$model_group",'
                'inference_foretoken_io_model_role=~"$model_role"}, engine)',
            )
        )
        .with_variable(
            variable(
                "model_service",
                "Autoscaling service",
                'label_values(foretoken_autoscaling_applied_replicas{namespace=~"$namespace",model_name=~"$model_name"}, modelservice)',
            )
        )
    )

    frontend_request_rates = frontend_metric("http_requests_total", rate=True)
    frontend_5xx_rates = frontend_metric("http_requests_total", extra='status="5xx"', rate=True)

    board.with_row(dashboard.Row("Overview"))
    board.with_panel(
        headline(
            "Model scrape targets",
            "Prometheus targets currently reporting for the selected model groups and roles.",
            f"sum({scoped_group_metric(f'foretoken:model_server_up:sum{{{GROUP}}}')})",
            color=None,
            thresholds=steps((None, RED), (1, GREEN)),
        )
    )
    board.with_panel(
        headline(
            "Prompt tokens / s",
            "Prompt tokens processed per second by the selected model servers.",
            f"sum({model_metric('vllm:prompt_tokens_total', rate=True)})",
            unit="suffix: token/s",
            interval="5s",
        )
    )
    board.with_panel(
        headline(
            "Output tokens / s",
            "Generated tokens produced per second by the selected model servers.",
            f"sum({model_metric('vllm:generation_tokens_total', rate=True)})",
            unit="suffix: token/s",
            color=ORANGE,
            interval="5s",
        )
    )
    board.with_panel(
        headline(
            "Scheduler queued requests",
            "Requests waiting in the selected model engines, not the shared frontend admission queue.",
            f"sum({model_metric('vllm:num_requests_waiting')})",
            color=None,
            thresholds=steps((None, GREEN), (1, ORANGE)),
            interval="5s",
        )
    )

    board.with_row(dashboard.Row("Model Serving"))
    board.with_panel(
        series(
            "Completed request rate",
            "Completed execution requests by backend and finish reason. A backend is one model instance and data-parallel rank; multi-stage requests can complete once in each role.",
            [
                foretoken_query(
                    f"sum by(model_name,model_group,model_role,engine,finished_reason) ({model_metric('vllm:request_success_total', rate=True)})",
                    "{{model_name}} / {{model_group_display}} / {{model_role}} / rank {{engine}} / {{finished_reason}}",
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
            "Prompt and generated tokens per second by backend. A backend is one model instance and data-parallel rank.",
            [
                foretoken_query(f"sum by(model_name,model_group,model_role,engine) ({model_metric('vllm:prompt_tokens_total', rate=True)})", "Prompt / {{model_name}} / {{model_group_display}} / {{model_role}} / rank {{engine}}"),
                foretoken_query(f"sum by(model_name,model_group,model_role,engine) ({model_metric('vllm:generation_tokens_total', rate=True)})", "Output / {{model_name}} / {{model_group_display}} / {{model_role}} / rank {{engine}}"),
            ],
            unit="suffix: token/s",
            span=8,
            colors={"Prompt": BLUE, "Output": ORANGE},
        )
    )
    board.with_panel(
        series(
            "Scheduler state",
            "Requests running in vLLM execution batches or waiting in its scheduler.",
            [
                foretoken_query(f"sum by(model_name,model_group,model_role,engine) ({model_metric('vllm:num_requests_running')})", "Running / {{model_group_display}} / {{model_role}} / rank {{engine}}"),
                foretoken_query(f"sum by(model_name,model_group,model_role,engine) ({model_metric('vllm:num_requests_waiting')})", "Waiting / {{model_group_display}} / {{model_role}} / rank {{engine}}"),
            ],
            unit="short",
            span=8,
            colors={"Running": BLUE, "Waiting": ORANGE},
        )
    )
    board.with_panel(
        latency(
            model_metric("vllm:e2e_request_latency_seconds_bucket", rate=True),
            "End-to-end latency (E2EL)",
            "Request-weighted latency from frontend processing to generation completion across the selected engines. Excludes downstream client delivery; clocks must be synchronized.",
            unit="suffix: s",
        )
    )
    board.with_panel(
        latency(
            model_metric("vllm:time_to_first_token_seconds_bucket", rate=True),
            "Time to first token (TTFT)",
            "Request-weighted time from frontend processing to the first token at model-server across the selected engines. Excludes downstream client delivery.",
            unit="suffix: s",
        )
    )
    board.with_panel(
        latency(
            model_metric("vllm:request_time_per_output_token_seconds_bucket", rate=True),
            "Time per output token (TPOT)",
            "Request-weighted TPOT across selected engines, in milliseconds. Each request contributes its average output-token interval.",
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
            "Inter-token latency (ITL)",
            "Output-token interval distribution across selected engines, in milliseconds. Each token interval is one observation.",
            unit="suffix: ms",
            scale=1_000,
        )
    )
    board.with_panel(
        series(
            "Request time by stage",
            "P95 time, in seconds, a request spends waiting for the scheduler, in prefill, and in decode.",
            [
                foretoken_query(
                    f"histogram_quantile(0.95, sum by(model_name,model_role,le) ({model_metric(metric, rate=True)}))",
                    "{{model_name}} / {{model_role}} / " + stage,
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
            "Scheduler preemptions per second by backend; sustained activity indicates KV-cache pressure.",
            [foretoken_query(f"sum by(model_name,model_group,model_role,engine) ({model_metric('vllm:num_preemptions_total', rate=True)})", "{{model_name}} / {{model_group_display}} / {{model_role}} / rank {{engine}}")],
            unit="ops",
            span=8,
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
            "KV-cache occupancy by model instance and engine rank.",
            [query(f"max by(model_name,model_group,model_role,engine) ({model_metric('vllm:kv_cache_usage_perc')})", "{{model_name}} / {{model_group_display}} / {{model_role}} / rank {{engine}}")],
            unit="percentunit",
            span=12,
        )
    )
    board.with_panel(
        series(
            "Prefix Cache hit ratio",
            "Cache hits divided by queried tokens across selected engines. Idle or unavailable caches have no ratio; local and external observations are separate.",
            [
                foretoken_query(
                    model_rate_ratio("vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total"),
                    "{{model_name}} / {{model_role}} / local",
                ),
                foretoken_query(
                    model_rate_ratio(
                        "vllm:external_prefix_cache_hits_total",
                        "vllm:external_prefix_cache_queries_total",
                    ),
                    "{{model_name}} / {{model_role}} / external",
                ),
            ],
            unit="percentunit",
            span=12,
            colors={"local": BLUE, "external": TEAL},
        )
    )
    board.with_panel(
        series(
            "Storage usage",
            "Mounted RuntimeCache filesystem utilization. Series are absent for model groups without a RuntimeCache.",
            [query(scoped_group_metric(f"foretoken:model_server_runtime_cache_usage_ratio:max{{{GROUP}}}"), "{{model_group_display}} / {{model_role}}")],
            unit="percentunit",
            span=12,
        )
    )
    board.with_panel(
        series(
            "Available storage",
            "Filesystem space available to model-server processes using a RuntimeCache.",
            [
                query(
                    scoped_group_metric(f"foretoken:model_server_runtime_cache_available_bytes:min{{{GROUP}}}"),
                    "{{model_group_display}} / {{model_role}}",
                )
            ],
            unit="bytes",
            span=12,
        )
    )

    board.with_row(dashboard.Row("Accelerators and Resources"))
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
    container = 'namespace=~"$namespace",container="model-server"'
    # Group membership includes workers that do not expose the head's engine metrics.
    selected_pods = (
        f"max by(namespace,pod) (foretoken:accelerator_workload_labels{{{GROUP}}} "
        f"and on(namespace,model_group) ({selected_groups()}))"
    )
    board.with_panel(
        series(
            "Serving CPU usage",
            "CPU cores used by model-server Pods belonging to the selected model instances.",
            [
                query(
                    f"sum by(namespace,pod) (rate(container_cpu_usage_seconds_total{{{container}}}[$__rate_interval])) and on(namespace,pod) ({selected_pods})",
                    "{{namespace}} / {{pod}}",
                    interval="30s",
                )
            ],
            unit="cores",
            span=12,
            colors={"frontend": BLUE, "model-server": ORANGE},
        )
    )
    board.with_panel(
        series(
            "Serving memory usage",
            "Working-set memory of model-server Pods belonging to the selected model instances.",
            [
                query(
                    f"sum by(namespace,pod) (container_memory_working_set_bytes{{{container}}}) and on(namespace,pod) ({selected_pods})",
                    "{{namespace}} / {{pod}}",
                    interval="30s",
                )
            ],
            unit="bytes",
            span=12,
            colors={"frontend": BLUE, "model-server": ORANGE},
        )
    )

    board.with_row(
        dashboard.Row("NVIDIA power and temperature")
        .with_panel(by_device(
            "NVIDIA GPU power by device",
            "Power draw of each Foretoken-attributed NVIDIA GPU.",
            "foretoken:accelerator_gpu_power_watts",
            unit="watt",
        ))
        .with_panel(by_device(
            "NVIDIA GPU temperature by device",
            "Temperature of each Foretoken-attributed NVIDIA GPU.",
            "foretoken:accelerator_gpu_temperature_celsius",
            unit="celsius",
        ))
    )

    # Route distribution uses the smallest routable unit; controller internals remain diagnostic.
    board.with_row(dashboard.Row("Routing decisions"))
    selections = routing_target_rates()
    selected_ranks = (
        'max by(namespace,model_group,data_parallel_rank) ('
        f'label_replace(0 * ({model_metric("vllm:kv_cache_usage_perc")}) + 1, '
        '"data_parallel_rank", "$1", "engine", "(.+)"))'
    )
    shares = (
        f"({selections}) / on(namespace,model_name,model_role) group_left() "
        f"(sum by(namespace,model_name,model_role) ({selections}) > 0)"
    )
    board.with_panel(
        series(
            "Routing share by backend",
            "Share of routing selections within each model and execution role. Each backend is one model instance and data-parallel rank; all backends form the denominator. This counts choices, not completed requests.",
            [foretoken_query(
                f"({shares}) and on(namespace,model_group,data_parallel_rank) ({selected_ranks})",
                "{{model_name}} / {{model_role}} / {{model_group_display}} / rank {{data_parallel_rank}}",
            )],
            unit="percentunit", span=24,
        ).height(10)
    )
    board.with_panel(
        series(
            "Routing outcomes",
            "Routing results observed within the selected time range, grouped by selection round.",
            [
                foretoken_query(
                    f"sum by(model_name,round,outcome) (rate(foretoken_router_selections_total{{{ROUTER}}}[$__rate_interval])) "
                    f"and on(model_name,round,outcome) (sum by(model_name,round,outcome) "
                    f"(increase(foretoken_router_selections_total{{{ROUTER}}}[$__range] @ end())) > 0)",
                    "{{model_name}} / {{round}} / {{outcome}}",
                )
            ],
            unit="reqps",
            span=8,
        )
    )
    board.with_panel(
        series(
            "Routing stage latency",
            "P99 filter, scorer and picker execution time, aggregated from histogram buckets "
            "across selected Frontend replicas.",
            [
                foretoken_query(
                    "histogram_quantile(0.99, sum by(model_name,round,stage,algorithm,le) "
                    f"(rate(foretoken_router_stage_duration_seconds_bucket{{{ROUTER}}}[$__rate_interval]))) >= 0",
                    "{{model_name}} / {{round}} / {{stage}} / {{algorithm}}",
                )
            ],
            unit="s",
            span=8,
        )
    )
    board.with_panel(
        series(
            "Eligible instances and ranks",
            "Mean available, filtered and selectable candidate counts. Selectable counts include data-parallel ranks.",
            [
                foretoken_query(
                    f"sum by(model_name,round,stage) (rate(foretoken_router_candidates_sum{{{ROUTER}}}[$__rate_interval])) "
                    f"/ (sum by(model_name,round,stage) (rate(foretoken_router_candidates_count{{{ROUTER}}}[$__rate_interval])) > 0)",
                    "{{model_name}} / {{round}} / {{stage}}",
                )
            ],
            unit="short",
            span=8,
        )
    )
    board.with_row(dashboard.Row("Shared frontend"))
    board.with_panel(
        headline(
            "Frontend scrape targets",
            "Prometheus targets currently reporting for the selected Frontend services.",
            f"sum(foretoken:frontend_up:sum{{{FRONTEND}}})",
            color=None,
            thresholds=steps((None, RED), (1, GREEN)),
        )
    )
    board.with_panel(
        headline(
            "Frontend response starts / s",
            "Frontend responses started per second over the selected rate window.",
            f"sum({frontend_request_rates})",
            unit="reqps",
            interval="5s",
        )
    )
    board.with_panel(
        headline(
            "Frontend HTTP 5xx ratio",
            "HTTP responses that started with 5xx divided by all started responses. "
            "Streaming failures after headers are not included.",
            f"(sum({frontend_5xx_rates}) or 0 * sum({frontend_request_rates})) "
            f"/ (sum({frontend_request_rates}) > 0)",
            unit="percentunit",
            color=None,
            thresholds=steps((None, GREEN), (0.01, AMBER), (0.05, RED)),
            interval="5s",
        )
    )
    board.with_panel(
        headline(
            "Frontend queued requests",
            "Requests waiting for frontend admission to a scaling target.",
            f"sum(foretoken:frontend_upstream_queued_requests:sum{{{FRONTEND}}})",
            color=None,
            thresholds=steps((None, GREEN), (1, ORANGE)),
        )
    )
    board.with_panel(
        series(
            "Frontend responses by HTTP status",
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
            "Frontend responses by endpoint",
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
                extra='handler=~"/v1/(chat/completions|completions|generate|messages|responses)"',
                rate=True,
            ),
            "Frontend response-header latency",
            "Time, in seconds, until the Frontend handler produces HTTP response headers. "
            "This excludes SSE body delivery; it is neither TTFT nor full-stream duration.",
            unit="suffix: s",
            span=6,
            dimensions="",
        )
    )
    board.with_panel(
        series(
            "Frontend admission queue",
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

    board.with_panel(
        series(
            "Frontend cache-index health",
            "Healthy KV event sources divided by configured sources. Disabled or unavailable indexing reports zero.",
            [query(f"foretoken:frontend_kv_index_source_health_ratio:min{{{FRONTEND}}}", "{{frontend_service}}")],
            unit="percentunit",
            span=8,
        )
    )
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
            'Queued reconciliations, deduplicated across controller replicas.',
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

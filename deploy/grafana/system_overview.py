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

from grafana_foundation_sdk.builders import (
    common,
    dashboard,
    heatmap,
    prometheus,
    stat,
    table,
    text,
    timeseries,
)
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models import common as models
from grafana_foundation_sdk.models import dashboard as dashboard_models
from grafana_foundation_sdk.models import heatmap as heatmap_models
from grafana_foundation_sdk.models import prometheus as prometheus_models
from grafana_foundation_sdk.models import text as text_models

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
    "Reading this dashboard": "看板读法",
    "Select a model for inference metrics or a frontend for traffic and admission. Adjust the time range to inspect trends.":
        "查看推理指标时选择模型；查看流量和准入时选择前端。调整时间范围查看趋势。",
    "No data": "无数据",
    "Generation latency": "生成延迟",
    "Request lengths": "请求长度分布",
    "Request latency samples / s": "请求采样数量",
    "Token interval samples / s": "Token 间隔采样数量",
    "TTFT and E2EL histogram observations per second for each whole model.":
        "每个模型每秒记录的 TTFT 与 E2EL 直方图样本数。",
    "Output-token interval observations per second for each whole model. These count token intervals.":
        "每个模型每秒记录的输出 token 间隔样本数。",
    "{{model_name}} / local": "{{model_name}} / 本地",
    "{{model_name}} / external": "{{model_name}} / 外部",
    "{{model_name}} / mean": "{{model_name}} / 均值",
    "{{model_name}} / {{model_role}} / queue": "{{model_name}} / {{model_role}} / 排队",
    "{{model_name}} / {{model_role}} / prefill": "{{model_name}} / {{model_role}} / 预填充",
    "{{model_name}} / {{model_role}} / decode": "{{model_name}} / {{model_role}} / 解码",
    "Shared frontend": "共享前端",
    "Model Serving": "模型服务",
    "Cache": "缓存",
    "Accelerators and Resources": "加速器与资源",
    "Routing decisions": "路由决策",
    "Control plane": "控制面",
    "Autoscaling decisions": "扩缩容决策",
    "Online frontend replicas": "在线前端副本数",
    "Online model servers": "在线模型服务数",
    "Frontend response starts / s": "HTTP 响应速率",
    "Input throughput": "输入吞吐量",
    "Output throughput": "输出吞吐量",
    "Frontend responses by HTTP status": "HTTP 响应速率（按状态码）",
    "Frontend responses by endpoint": "HTTP 响应速率（按接口）",
    "Frontend response-header latency": "HTTP 响应头延迟",
    "Model preparation and dispatch wait": "模型准备与派发等待",
    "Completed request rate": "完成请求速率",
    "Total / {{model_name}}": "模型总计 / {{model_name}}",
    "Running total / {{model_name}}": "运行总数 / {{model_name}}",
    "Waiting total / {{model_name}}": "排队总数 / {{model_name}}",
    "Whole-model totals across every instance and rank, with selected backend details. Totals ignore instance, role and rank filters.":
        "粗线展示每个模型全部实例和引擎编号的总吞吐量，细线展示所选后端。模型总计不随实例、角色或引擎编号筛选缩小。",
    "Scheduler state": "调度器状态",
    "End-to-end latency (E2EL)": "端到端延迟 (E2EL)",
    "Time to first token (TTFT)": "首 token 延迟 (TTFT)",
    "Time per output token (TPOT)": "每输出 token 耗时 (TPOT)",
    "Inter-token latency (ITL)": "相邻 token 间隔 (ITL)",
    "Stage latency (p95)": "阶段耗时（P95）",
    "Preemption events / s": "抢占事件速率",
    "Speculative decoding": "推测解码",
    "Draft and accepted tokens / s": "草稿与接受 token 速率",
    "Draft acceptance ratio": "草稿 token 接受率",
    "Accepted tokens per draft iteration": "每次草稿迭代接受 token 数",
    "Acceptance probability by position": "各草稿位置接受概率",
    "Speculative stage GPU time": "推测解码阶段 GPU 时间",
    "Speculative GPU time shares": "推测解码 GPU 时间占比",
    "Timed speculative steps / s": "推测解码计时步速率",
    "Prompt length": "输入长度",
    "Output length": "输出长度",
    "KV Cache utilization": "KV 缓存使用率",
    "Prefix Cache hit ratio": "前缀缓存命中率",
    "Frontend cache-index health": "前端缓存索引健康度",
    "Runtime cache filesystem usage": "运行缓存文件系统使用率",
    "Runtime cache filesystem free space": "运行缓存文件系统可用空间",
    "GPU utilization by device": "各设备 GPU 使用率",
    "GPU memory by device": "各设备 GPU 显存使用率",
    "GPU power by device": "各设备 GPU 功耗",
    "GPU temperature by device": "各设备 GPU 温度",
    "Serving CPU usage": "服务 CPU 使用量",
    "Serving memory usage": "服务内存使用量",
    "Routing outcomes": "路由选择速率（按结果）",
    "Routing stage latency (p99)": "路由阶段耗时（P99）",
    "Eligible instances and ranks": "路由候选数量（请求平均）",
    "Reconcile errors": "协调错误",
    "Reconcile latency (p99)": "协调耗时（P99）",
    "Controller workqueues": "控制器工作队列",
    "Replica decisions": "副本决策",
    "Serving capacity": "服务容量",
    "Observation and evaluation age": "距最近观测与评估",
    "Latest autoscaling stage": "最新扩缩容阶段",
    "Data source": "数据源",
    "Namespace": "命名空间",
    "Frontend service": "前端服务",
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
    "Number of successfully scraped frontend replicas in the selected services.":
        "所选前端服务中指标抓取成功的副本数。",
    "Number of online model servers in the selected model groups and roles.":
        "所选模型组和执行角色中的在线模型服务数。",
    "Frontend responses started per second over the selected rate window.": "选定速率窗口内每秒开始的 HTTP 响应数。",
    "Input tokens per second for each whole model, across all instances and ranks.": "每个模型全部实例和引擎编号每秒处理的输入 token 总数。",
    "Output tokens per second for each whole model, across all instances and ranks.": "每个模型全部实例和引擎编号每秒生成的输出 token 总数。",
    "Frontend response starts grouped by HTTP status class.": "按 HTTP 状态类别分组的响应速率。",
    "Frontend response starts grouped by HTTP endpoint.": "按 HTTP 接口分组的响应速率。",
    "Time to HTTP response headers, in seconds.":
        "收到 HTTP 响应头的等待时间，单位秒。",
    "Requests waiting for runtime preparation or backend dispatch, grouped by scaling-target kind.":
        "按扩缩容目标类型分组，等待运行时准备或后端派发的请求数。",
    "Completed generation requests per second, with backend details.":
        "每秒完成的生成请求数及后端明细。",
    "Whole-model running and queued execution totals across all roles, with selected backend details.":
        "每个模型全部执行角色的运行与排队总数，并展示所选后端明细。",
    "Whole-model latency from Frontend processing to generation completion, in seconds; aggregate and decode requests are combined.":
        "从前端开始处理请求到生成完成的耗时，按模型统计，单位为秒。",
    "Whole-model time from Frontend processing to the first output token, in seconds; aggregate and decode requests are combined.":
        "从前端开始处理请求到首个输出 token 的耗时，按模型统计，单位为秒。",
    "Whole-model time per output token, in milliseconds; each request contributes its average interval.":
        "每个请求的平均输出 token 间隔，按模型统计分位数和均值，单位为毫秒。",
    "Time between output tokens, in milliseconds.":
        "相邻输出 token 的时间间隔，单位毫秒。",
    "Draft and accepted token rates for each whole model.":
        "每个模型的草稿与接受 token 速率。",
    "Accepted draft tokens divided by proposed draft tokens across all engines.":
        "全部引擎接受的草稿 token 数除以提出的草稿 token 数。",
    "Accepted draft tokens per draft iteration across all engines; excludes bonus tokens.":
        "全部引擎每次草稿迭代接受的草稿 token 数。",
    "Accepted tokens at each zero-based draft position divided by draft iterations across all engines.":
        "各草稿位置（从 0 开始）的接受数除以全部引擎草稿迭代数。",
    "Draft / {{model_name}}": "草稿 / {{model_name}}",
    "Accepted / {{model_name}}": "接受 / {{model_name}}",
    "Position {{position}} / {{model_name}}": "位置 {{position}} / {{model_name}}",
    "Target forward / {{model_name}}": "目标模型前向计算 / {{model_name}}",
    "Draft share / {{model_name}}": "草稿占比 / {{model_name}}",
    "Target forward share / {{model_name}}": "目标模型前向计算占比 / {{model_name}}",
    "Average draft and target-forward time per measured step.":
        "每个计时步中草稿与目标模型前向计算的平均耗时。",
    "Share of measured time spent on drafting and target forward.":
        "草稿与目标模型前向计算各自的耗时占比。",
    "Measured speculative decoding steps per second.":
        "每秒计时的推测解码步数。",
    "ITL / {{model_name}}": "ITL / {{model_name}}",
    "TTFT / {{model_name}}": "TTFT / {{model_name}}",
    "E2EL / {{model_name}}": "E2EL / {{model_name}}",
    "P95 queue, prefill and decode time for completed requests, in seconds.":
        "已完成请求的排队、预填充和解码耗时 P95，单位秒。",
    "Whole-model preemption events per second across every engine, with selected backend details.":
        "每个模型全部引擎每秒发生的抢占事件总数，并展示所选后端明细。",
    "Distribution of prompt tokens per request across selected engines.": "所选引擎每次请求的输入 token 数分布。",
    "Distribution of generated tokens per request across selected engines.": "所选引擎每次请求的输出 token 数分布。",
    "KV-cache occupancy by model instance and engine rank.": "按模型实例和引擎编号展示 KV 缓存占用率。",
    "Whole-model cache hits divided by queried tokens. Local and external caches are separate.":
        "模型整体命中 token 数除以查询 token 数；本地和外部缓存分开统计。",
    "Healthy KV event sources divided by configured sources.":
        "健康 KV 事件源数除以已配置源数。",
    "Highest RuntimeCache filesystem usage by model instance.": "各模型实例缓存文件系统的最高使用率。",
    "Lowest available RuntimeCache filesystem space by model instance.": "各模型实例缓存文件系统的最少可用空间。",
    "Utilization of each GPU used by the selected model.": "所选模型所在 GPU 的使用率。",
    "Memory utilization of each GPU used by the selected model.": "所选模型所在 GPU 的显存使用率。",
    "Power draw of each GPU used by the selected model, in watts.":
        "所选模型所在 GPU 的功耗，单位为 W。",
    "Temperature reported by each GPU used by the selected model; MetaX uses the chip hotspot sensor.":
        "所选模型所在 GPU 报告的温度；沐曦使用芯片热点测点。",
    "CPU cores used by all model-server Pods of each model, with selected instance and node details.": "每个模型全部模型服务器 Pod 使用的 CPU 核数，并展示所选实例和节点的明细。",
    "Working-set memory of all model-server Pods of each model, with selected instance and node details.": "每个模型全部模型服务器 Pod 的工作集内存，并展示所选实例和节点的明细。",
    "Routing selection rate by stage and outcome within the selected time range.":
        "所选时间范围内每秒路由选择次数，按阶段和结果分组。",
    "P99 filter, scorer, and picker time across selected Frontend replicas.":
        "所选前端副本的筛选、评分和选择阶段 P99 耗时。",
    "Mean available, filtered, and selectable candidate counts per routing selection; selectable includes data-parallel ranks.":
        "每次路由选择中可用、筛选后和可选候选的平均数量；可选候选包含数据并行副本。",
    "Reconciliation errors per second by controller.": "各控制器每秒协调错误数。",
    "P99 reconciliation time by controller.": "各控制器协调耗时的 P99。",
    "Depth of each controller workqueue.": "各控制器工作队列深度。",
    "Published recommendation, adjusted target, and applied capacity.":
        "已发布的建议值、调整后目标和已应用容量。",
    "Ready and routable capacity compared with applied desired replicas.":
        "已就绪、可路由与期望副本数。",
    "Age in seconds since the latest observation and evaluation.":
        "距最近一次观测和评估的秒数。",
    "Latest trigger, decision, and adjustment outcomes for the selected model service.":
        "所选模型服务最近一次扩缩容评估的触发、决策和调整结果。",
    "Engine rank": "引擎编号",
    "Routing share by backend": "各后端路由占比",
    "Routing share by backend within each model and execution role.":
        "各模型、执行角色内的后端路由占比。",
    "Scheduler queued requests": "引擎排队请求",
    "Queued execution requests across all instances, roles and ranks of each model.":
        "每个模型全部实例、角色和引擎编号中等待调度的执行请求总数。",
    "Running / {{model_group_display}} / rank {{engine}}":
        "运行中 / {{model_group_display}} / 编号 {{engine}}",
    "Waiting / {{model_group_display}} / rank {{engine}}":
        "等待中 / {{model_group_display}} / 编号 {{engine}}",
    "Frontend pod": "前端 Pod",
    "Admission": "准入",
    "Admission results and wait": "准入结果与等待",
    "Admission resources": "准入资源",
    "Admission replicas": "准入指标副本数",
    "Online / {{namespace}} / {{frontend_service}}": "在线 / {{namespace}} / {{frontend_service}}",
    "Covered / {{namespace}} / {{frontend_service}}": "指标完整 / {{namespace}} / {{frontend_service}}",
    "Online frontend replicas and those with complete admission metrics.":
        "在线前端副本数及其中指标完整的副本数。",
    "Intake calls / s": "入口准入调用速率",
    "HTTP requests entering admission per second.":
        "每秒进入准入的 HTTP 请求数。",
    "Work admitted calls / s": "工作准入获准速率",
    "HTTP requests admitted for processing per second; batches count once.":
        "每秒获准处理的 HTTP 请求数，批次计一次。",
    "Capacity rejection ratio": "容量拒绝比例",
    "Share of completed admission calls rejected for capacity, by stage.":
        "各准入阶段已结束的调用中，因容量不足被拒绝的占比。",
    "Work timeout ratio": "工作准入超时比例",
    "Share of completed work-admission calls that timed out.":
        "已结束的工作准入调用中，超时调用的占比。",
    "Admitted queue wait (p95)": "获准排队等待（P95）",
    "Queue-wait p95 for HTTP requests that were admitted.":
        "排队后获准的 HTTP 请求等待 P95。",
    "No samples": "无样本",
    "Unlimited": "无限流",
    "Concurrency": "并发限流",
    "No queue": "不排队",
    "Admission by replica": "各副本准入状态",
    "Compare admission status and results across frontend replicas.":
        "对比各前端副本的准入状态与结果。",
    "Current usage and configured limits for each frontend replica.":
        "各前端副本的当前占用和配置上限。",
    "Intake results / s": "入口准入结果速率",
    "Work calls and results / s": "工作准入到达与结果速率",
    "{{namespace}} / {{frontend_service}} / {{origin}} / arrivals": "{{namespace}} / {{frontend_service}} / {{origin}} / 到达",
    "Admission capacity by replica": "各副本准入容量",
    "Admission calls per second by result, with work arrivals shown separately.":
        "各结果的准入调用速率，另列工作准入到达速率。",
    "Queue wait by result": "各结果的排队等待",
    "Compare queue waits for admitted, timed-out and cancelled calls.":
        "对比获准、超时和取消调用的排队等待。",
    "Queue exit samples / s": "排队退出样本速率",
    "Completed queue-wait observations per second, grouped by result.":
        "按结果展示每秒结束的排队等待样本数。",
    "Active work units": "活跃工作单位",
    "Queued work units": "排队工作单位",
    "Resident HTTP requests": "驻留 HTTP 请求",
    "Admitted work units and their concurrency limit.":
        "已准入的工作单位及并发上限。",
    "Waiting work units and the queue limit.":
        "等待中的工作单位及队列上限。",
    "HTTP requests still being handled and the residency limit.":
        "仍在处理的 HTTP 请求数及驻留上限。",
    "Occupancy / {{namespace}} / {{frontend_service}}": "占用 / {{namespace}} / {{frontend_service}}",
    "Limit / {{namespace}} / {{frontend_service}}": "上限 / {{namespace}} / {{frontend_service}}",
    "Replica": "副本",
    "Admission rule": "准入规则",
    "Scrape": "抓取",
    "Telemetry complete": "指标完整",
    "Concurrency limit": "并发上限",
    "Queue limit": "队列上限",
    "Resident requests": "驻留请求",
    "Resident limit": "驻留上限",
    "Intake rejection": "入口拒绝比例",
    "Work rejection": "工作拒绝比例",
    "Work timeout": "工作超时比例",
    "Admitted wait p95": "获准等待 P95",
}

TABLE_COLUMNS_ZH = {
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
RAW_MODEL_ALL = (
    'endpoint="model-server",namespace=~"$namespace",model_name=~"$model_name",'
    'inference_foretoken_io_model_group!="",inference_foretoken_io_model_role!=""'
)
RAW_MODEL = (
    RAW_MODEL_ALL + ',inference_foretoken_io_model_group=~"$model_group",'
    'inference_foretoken_io_model_role=~"$model_role",engine=~"$engine"'
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
    """Join immutable scrape identity onto series without changing their Group keys."""
    # Keep names for retired instances throughout the selected history. During a rollout,
    # creation time distinguishes successive executions of the same service/Pool slot.
    identities = 'max_over_time(foretoken:model_instance_info{namespace=~"$namespace"}[$__range] @ end())'
    names = f'label_join({identities}, "model_group_display", " / ", "modelservice", "model_pool", "model_instance")'
    duplicates = f'count by(model_group_display) ({names}) > 1'
    # Match duplicates before replacing their display label.
    distinct_names = (
        f'label_join(({names}) and on(model_group_display) ({duplicates}), '
        '"model_group_display", " / ", "model_group_display", "namespace", "model_instance_created_at")'
    )
    display = f'max by(namespace,model_group,model_group_display) (({distinct_names}) or (({names}) unless on(model_group_display) ({duplicates})))'
    return f'({expr}) * on(namespace,model_group) group_left(model_group_display) ({display})'


def query(
    expr: str, legend: str | None = None, *, interval: str | None = None, instant: bool = False
) -> prometheus.Dataquery:
    """Build one Prometheus query for dashboard time series or current-value tiles."""
    if legend is not None and "{{model_group_display}}" in legend:
        expr = instance_display(expr)
    target = prometheus.Dataquery().datasource(PROMETHEUS).expr(expr)
    if instant:
        target.instant()
    else:
        target.range()
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


def model_metric(metric: str, *, extra: str = "", rate: bool = False, whole_model: bool = False) -> str:
    """Normalize engine labels; whole-model queries ignore backend drill-down filters."""
    expr = _selector(metric, RAW_MODEL_ALL if whole_model else RAW_MODEL, extra)
    if rate:
        expr = f"rate({expr}[$__rate_interval])"
    return (
        f'label_replace(label_replace(label_replace({expr}, '
        '"model_group", "$1", "inference_foretoken_io_model_group", "(.+)"), '
        '"model_role", "$1", "inference_foretoken_io_model_role", "(.+)"), '
        '"pd_pipeline_scope", "$1", "inference_foretoken_io_pd_pipeline_scope", "(.+)")'
    )


def model_total(metric: str, *, rate: bool = False, roles: str = "") -> str:
    """Aggregate every backend of each selected model, with stage roles chosen by the metric."""
    extra = f'inference_foretoken_io_model_role=~"{roles}"' if roles else ""
    return f"sum by(model_name) ({model_metric(metric, extra=extra, rate=rate, whole_model=True)})"


def selected_groups() -> str:
    """Resolve model identity from Service scrape targets even when engine metrics are absent."""
    return 'max by(namespace,model_group) (foretoken:model_instance_info{namespace=~"$namespace",model_name=~"$model_name",model_group=~"$model_group"})'


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
    numerator = model_metric(numerator_metric, rate=True, whole_model=True)
    denominator = model_metric(denominator_metric, rate=True, whole_model=True)
    return f"sum by(model_name) ({numerator}) / (sum by(model_name) ({denominator}) > 0)"


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
    legend: str | None = None,
) -> stat.Panel:
    """Show the latest value of each summary series, using thresholds when configured."""
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
        .graph_mode(models.BigValueGraphMode.NONE)
        .wide_layout(False)
        .justify_mode(models.BigValueJustifyMode.CENTER)
        .text(common.VizTextDisplayOptions().title_size(12).value_size(32))
        .text_mode(models.BigValueTextMode.VALUE_AND_NAME if legend else models.BigValueTextMode.VALUE)
        .reduce_options(common.ReduceDataOptions().calcs(["lastNotNull"]))
        .with_target(query(expr, legend or title, interval=interval or "5s", instant=True).ref_id("A"))
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
        .min(0)
        .no_value("No data")
        .line_width(2)
        .fill_opacity(24 if stack else 8)
        .point_size(8)
        .show_points(models.VisibilityMode.NEVER)
        .legend(
            common.VizLegendOptions()
            .display_mode(models.LegendDisplayMode.LIST)
            .placement(models.LegendPlacement.BOTTOM)
            .show_legend(True)
        )
        .tooltip(common.VizTooltipOptions().mode(models.TooltipDisplayMode.MULTI).sort(models.SortOrder.DESCENDING))
        .targets([target.ref_id(chr(ord("A") + index)) for index, target in enumerate(targets)])
        .span(span)
        .height(10)
    )
    if unit == "percentunit":
        panel.min(0).max(1)
    if stack:
        panel.stacking(common.StackingConfig().mode(models.StackingMode.NORMAL).group("A"))
    # Bind semantic colors to queries before legend localization; dynamic status labels use names.
    for name, color in (colors or {}).items():
        value = [dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": color})]
        matched = False
        for index, target in enumerate(targets):
            legend = target.build().legend_format or ""
            if legend == name or legend.endswith(" / " + name):
                panel.override_by_query(chr(ord("A") + index), value)
                matched = True
        if not matched:
            panel.override_by_name(name, value)
    return panel


def latency(
    bucket_rates: str,
    title: str,
    description: str,
    *,
    unit: str,
    scale: int = 1,
    mean_rates: tuple[str, str] | None = None,
    span: int = 12,
    dimensions: str = "model_name",
) -> timeseries.Panel:
    """A raw-histogram latency panel with fixed units and optional interval-local mean."""
    targets = []
    buckets = f"{dimensions},le" if dimensions else "le"
    prefix = "{{model_name}} / " if dimensions else ""
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
    panel = series(title, description, targets, unit=unit, span=span, colors=colors)
    return panel.decimals(2) if scale == 1 else panel


def distribution(title: str, description: str, metric: str) -> heatmap.Panel:
    """A heatmap of a raw request-length histogram using the selected rate window."""
    return (
        heatmap.Panel()
        .title(title)
        .description(description)
        .datasource(PROMETHEUS)
        .no_value("No data")
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
        .height(10)
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
    visible_keys = {"title", "description", "label", "legendFormat", "noValue", "text", "content"}
    if isinstance(value, dict):
        localized = {
            key: ZH.get(item, item) if key in visible_keys and isinstance(item, str) else localize_dashboard(item)
            for key, item in value.items()
        }
        if value.get("id") == "byName" and isinstance(value.get("options"), str):
            name = value["options"]
            localized["options"] = TABLE_COLUMNS_ZH.get(name, ZH.get(name, name))
        if value.get("id") == "organize" and isinstance(localized.get("options"), dict):
            options = localized["options"]
            names = dict.fromkeys(options.get("indexByName", {}))
            names.update(options.get("renameByName", {}))
            options["renameByName"] = {
                name: TABLE_COLUMNS_ZH.get(display or name, ZH.get(display or name, display or name))
                for name, display in names.items()
            }
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


def admission_panels(board: dashboard.Dashboard) -> None:
    """Add service-level admission summaries and Pod diagnostics from live frontend metrics.

    Intake and work keep separate result denominators. Capacity comes from the same Pods as
    occupancy, never desired configuration; availability anchors rows with missing telemetry.
    """
    pod_keys = "namespace,frontend_service,pod"
    service_keys = "namespace,frontend_service"
    service_legend = "{{namespace}} / {{frontend_service}}"

    def metric(name: str, extra: str = "", *, rate: bool = False) -> str:
        labels = 'pod=~"$frontend_pod"' + ("," + extra if extra else "")
        return frontend_metric(name, extra=labels, rate=rate)

    def results(stage: str, keys: str, result: str = "") -> str:
        labels = f'stage="{stage}",origin="http"'
        if result:
            labels += f',result=~"{result}"'
        return f"sum by({keys}) ({metric('foretoken_admission_results_total', labels, rate=True)})"

    def ratio(stage: str, keys: str, result: str) -> str:
        total = results(stage, keys)
        matched = results(stage, keys, result)
        return f"(({matched}) or (0 * ({total}))) / (({total}) > 0)"

    def wait(quantile: float, keys: str, extra: str) -> str:
        buckets = metric("foretoken_admission_queue_wait_seconds_bucket", extra, rate=True)
        return f"histogram_quantile({quantile}, sum by({keys},le) ({buckets}))"

    up = f"max by({pod_keys}) ({metric('up')})"
    info = f"max by({pod_keys},algorithm) ({metric('foretoken_admission_info')})"
    # Zero-event base counters must exist; histogram samples are required only after a wait.
    complete = info
    for stage in ("intake", "work"):
        for name in ("attempts_total", "results_total"):
            present = metric(f"foretoken_admission_{name}", f'stage="{stage}",origin="http"')
            complete = f"({complete}) and on({pod_keys}) ({present})"
    concurrency = metric("foretoken_admission_info", 'algorithm="concurrency"')
    bounded = f"({info}) and on({pod_keys}) ({concurrency})"
    for name in (
        "active_work_units", "queued_work_units", "resident_requests",
        "concurrency_limit_work_units", "queue_limit_work_units", "resident_limit_requests",
    ):
        bounded = f"({bounded}) and on({pod_keys}) ({metric('foretoken_admission_' + name)})"
    other_rules = f"({complete}) unless on({pod_keys}) ({concurrency})"
    covered = (
        f"max by({pod_keys}) (({other_rules}) or (({complete}) and on({pod_keys}) ({bounded}))) "
        f"and on({pod_keys}) (({up}) == 1)"
    )
    coverage = f"({covered}) or (0 * ({up}))"
    admitted_wait = wait(0.95, service_keys, 'origin="http",result="admitted"')

    board.with_row(dashboard.Row("Admission"))
    board.with_panel(
        headline(
            "Admission replicas",
            "Online frontend replicas and those with complete admission metrics.",
            f"sum by({service_keys}) ({up})", legend="Online / " + service_legend,
        ).targets([
            query(f"sum by({service_keys}) ({up})", "Online / " + service_legend, instant=True).ref_id("A"),
            query(f"sum by({service_keys}) ({coverage})", "Covered / " + service_legend, instant=True).ref_id("B"),
        ]).span(8).height(5)
    )
    intake_attempts = metric("foretoken_admission_attempts_total", 'stage="intake",origin="http"', rate=True)
    for title, description, expr in (
        (
            "Intake calls / s",
            "HTTP requests entering admission per second.",
            f"sum by({service_keys}) ({intake_attempts})",
        ),
        (
            "Work admitted calls / s",
            "HTTP requests admitted for processing per second; batches count once.",
            results("work", service_keys, "admitted"),
        ),
    ):
        board.with_panel(headline(title, description, expr, unit="suffix: calls/s", legend=service_legend).span(8).height(5))
    rejection = (
        f'label_replace(({ratio("intake", service_keys, "capacity_rejected")}), "stage", "intake", "", "") '
        f'or label_replace(({ratio("work", service_keys, "capacity_rejected")}), "stage", "work", "", "")'
    )
    for title, description, expr, unit, legend in (
        (
            "Capacity rejection ratio",
            "Share of completed admission calls rejected for capacity, by stage.",
            rejection, "percentunit", service_legend + " / {{stage}}",
        ),
        (
            "Work timeout ratio",
            "Share of completed work-admission calls that timed out.",
            ratio("work", service_keys, "queue_timeout|deadline_exceeded"), "percentunit", service_legend,
        ),
        (
            "Admitted queue wait (p95)",
            "Queue-wait p95 for HTTP requests that were admitted.",
            admitted_wait, "s", service_legend,
        ),
    ):
        board.with_panel(headline(title, description, expr, unit=unit, legend=legend, no_value="No samples").span(8).height(5))

    # Join on a namespace-qualified replica key, so equal Pod names cannot merge across services.
    live = f"({up}) == 1"
    unknown_rule = f'label_replace(0 * ({up}), "algorithm", "unreported", "", "")'
    identity = f"(({info}) and on({pod_keys}) ({live})) or on({pod_keys}) ({unknown_rule})"

    def replica_table(title: str, description: str, columns: list[tuple[str, str, str]]) -> table.Panel:
        """Join instant Pod observations without losing unavailable targets or mixing resource units."""
        targets = []
        for index, expr in enumerate([identity, *(expr for _, expr, _ in columns)]):
            # Restrict rows to discovered Pods; recent counters can outlive a removed target.
            # NaN cells keep unsampled columns without inventing zero values or extra rows.
            if index:
                expr = f"(({expr}) and on({pod_keys}) ({up})) or on({pod_keys}) (({up}) * (0 / 0))"
            joined = f'label_join(({expr}), "replica", " / ", "namespace", "frontend_service", "pod")'
            if index:
                joined = f"max by(replica) ({joined})"
            targets.append(
                query(joined, instant=True).format(prometheus_models.PromQueryFormat.TABLE).ref_id(chr(ord("A") + index))
            )
        names = {f"Value #{chr(ord('B') + index)}": name for index, (name, _, _) in enumerate(columns)}
        ordered = ["replica", "algorithm", *names]
        names.update({"replica": "Replica", "algorithm": "Admission rule"})
        no_data = dashboard_models.SpecialValueMap(
            options=dashboard_models.DashboardSpecialValueMapOptions(
                match=dashboard_models.SpecialValueMatch.NULL_AND_NAN,
                result=dashboard_models.ValueMappingResult(text="No data"),
            )
        )
        panel = (
            table.Panel().title(title).description(description).datasource(PROMETHEUS)
            .show_header(True).cell_height(models.TableCellHeight.SM).no_value("No data").mappings([no_data]).targets(targets)
            .with_transformation(dashboard_models.DataTransformerConfig(id_val="joinByField", options={"byField": "replica", "mode": "outerTabular"}))
            .with_transformation(dashboard_models.DataTransformerConfig(id_val="filterFieldsByName", options={"include": {"names": ordered}}))
            .with_transformation(dashboard_models.DataTransformerConfig(id_val="organize", options={
                "indexByName": {name: index for index, name in enumerate(ordered)},
                "renameByName": names,
            }))
            .override_by_name("Replica", [dashboard_models.DynamicConfigValue(id_val="custom.width", value=360)])
            .override_by_name("Admission rule", [dashboard_models.DynamicConfigValue(id_val="mappings", value=[{
                "type": "value", "options": {
                    "allow_all": {"text": "Unlimited"},
                    "concurrency": {"text": "Concurrency"},
                    "unreported": {"text": "No data"},
                },
            }])])
            .span(24).height(6)
        )
        for name, _, unit in columns:
            panel.override_by_name(name, [dashboard_models.DynamicConfigValue(id_val="unit", value=unit)])
        if any(name == "Queue limit" for name, _, _ in columns):
            panel.override_by_name("Queue limit", [dashboard_models.DynamicConfigValue(id_val="mappings", value=[
                no_data, {"type": "value", "options": {"0": {"text": "No queue"}}},
            ])])
        return panel

    board.with_panel(replica_table(
        "Admission by replica",
        "Compare admission status and results across frontend replicas.",
        [
            ("Scrape", up, "short"),
            ("Telemetry complete", coverage, "short"),
            ("Intake rejection", ratio("intake", pod_keys, "capacity_rejected"), "percentunit"),
            ("Work rejection", ratio("work", pod_keys, "capacity_rejected"), "percentunit"),
            ("Work timeout", ratio("work", pod_keys, "queue_timeout|deadline_exceeded"), "percentunit"),
            ("Admitted wait p95", wait(0.95, pod_keys, 'origin="http",result="admitted"'), "s"),
        ],
    ))

    details = dashboard.Row("Admission results and wait")
    for stage, title in (("intake", "Intake results / s"), ("work", "Work calls and results / s")):
        rates = metric("foretoken_admission_results_total", f'stage="{stage}"', rate=True)
        targets = [foretoken_query(
            f"sum by({service_keys},origin,result) ({rates})",
            service_legend + " / {{origin}} / {{result}}",
        )]
        if stage == "work":
            arrivals = metric("foretoken_admission_attempts_total", 'stage="work"', rate=True)
            targets.append(foretoken_query(
                f"sum by({service_keys},origin) ({arrivals})", service_legend + " / {{origin}} / arrivals",
            ))
        result_panel = series(
            title,
            "Admission calls per second by result, with work arrivals shown separately.",
            targets, unit="suffix: calls/s", span=12,
        )
        if stage == "work":
            # Arrivals include pending calls; distinguish offered demand from completed results.
            result_panel.override_by_query("B", [
                dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": "#808080"}),
                dashboard_models.DynamicConfigValue(id_val="custom.lineStyle", value={"fill": "dash", "dash": [6, 4]}),
                dashboard_models.DynamicConfigValue(id_val="custom.fillOpacity", value=0),
            ])
        details.with_panel(result_panel)
    details.with_panel(series(
        "Queue wait by result",
        "Compare queue waits for admitted, timed-out and cancelled calls.",
        [
            foretoken_query(wait(quantile, service_keys + ",origin,result", extra), service_legend + " / {{origin}} / {{result}} / " + label)
            for quantile, extra, label in (
                (0.50, 'result="admitted"', "p50"),
                (0.95, 'result="admitted"', "p95"),
                (0.95, 'result!="admitted"', "p95"),
            )
        ], unit="s", span=12,
    ))
    details.with_panel(series(
        "Queue exit samples / s",
        "Completed queue-wait observations per second, grouped by result.",
        [foretoken_query(
            f"sum by({service_keys},origin,result) ({metric('foretoken_admission_queue_wait_seconds_count', rate=True)})",
            service_legend + " / {{origin}} / {{result}}",
        )], unit="suffix: samples/s", span=12,
    ))
    board.with_row(details)

    resources = dashboard.Row("Admission resources")
    capacity_columns = []
    for name, suffix in (
        ("Active work units", "active_work_units"),
        ("Concurrency limit", "concurrency_limit_work_units"),
        ("Queued work units", "queued_work_units"),
        ("Queue limit", "queue_limit_work_units"),
        ("Resident requests", "resident_requests"),
        ("Resident limit", "resident_limit_requests"),
    ):
        observed = metric("foretoken_admission_" + suffix)
        capacity_columns.append((name, f"({observed}) and on({pod_keys}) ({live})", "short"))
    resources.with_panel(replica_table(
        "Admission capacity by replica",
        "Current usage and configured limits for each frontend replica.",
        capacity_columns,
    ))
    for title, description, occupancy, limit, unit in (
        (
            "Active work units",
            "Admitted work units and their concurrency limit.",
            "active_work_units", "concurrency_limit_work_units", "suffix: work units",
        ),
        (
            "Queued work units",
            "Waiting work units and the queue limit.",
            "queued_work_units", "queue_limit_work_units", "suffix: work units",
        ),
        (
            "Resident HTTP requests",
            "HTTP requests still being handled and the residency limit.",
            "resident_requests", "resident_limit_requests", "suffix: HTTP requests",
        ),
    ):
        usage = f"max by({pod_keys}) ({metric('foretoken_admission_' + occupancy)}) and on({pod_keys}) ({live})"
        capacity = f"max by({pod_keys}) ({metric('foretoken_admission_' + limit)}) and on({pod_keys}) ({live})"
        resources.with_panel(series(
            title, description,
            [
                foretoken_query(f"sum by({service_keys}) (({usage}) and on({pod_keys}) ({capacity}))", "Occupancy / " + service_legend),
                foretoken_query(f"sum by({service_keys}) (({capacity}) and on({pod_keys}) ({usage}))", "Limit / " + service_legend),
            ], unit=unit, span=8,
            colors={"Occupancy / " + service_legend: TEAL, "Limit / " + service_legend: BLUE},
        ))
    board.with_row(resources)


def build() -> dashboard_models.Dashboard:
    """Build both locales' shared operator view, ordered from model traffic to platform diagnostics."""
    instances = (
        'max by(namespace,model_group) ('
        'max_over_time(foretoken:model_instance_info{namespace=~"$namespace",'
        'model_name=~"$model_name"}[$__range] @ end()))'
    )
    board = (
        dashboard.Dashboard("Foretoken System Overview")
        .uid("foretoken-system-overview")
        .tags(["foretoken", "inference", "operations"])
        .editable()
        .tooltip(dashboard_models.DashboardCursorSync.CROSSHAIR)
        .refresh("5s")
        .time("now-15m", "now")
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
                "frontend_pod", "Frontend pod",
                'label_values(up{endpoint="http",namespace=~"$namespace",'
                'inference_foretoken_io_frontend_service=~"$frontend_service",'
                'inference_foretoken_io_frontend_service!=""}, pod)',
            )
        )
        .with_variable(
            variable(
                "model_name", "Model",
                'label_values(foretoken:model_instance_info{namespace=~"$namespace"}, model_name)',
            )
        )
        .with_variable(
            variable(
                "model_group",
                "Model instance",
                f"query_result({instance_display(instances)})",
            ).regex('/model_group="(?<value>[^"]+)".*model_group_display="(?<text>[^"]+)"/')
            .refresh(dashboard_models.VariableRefresh.ON_TIME_RANGE_CHANGED)
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

    board.with_panel(
        text.Panel()
        .title("Reading this dashboard")
        .mode(text_models.TextMode.MARKDOWN)
        .content(
            "Select a model for inference metrics or a frontend for traffic and admission. "
            "Adjust the time range to inspect trends."
        )
        .span(24)
        .height(4)
    )
    board.with_row(dashboard.Row("Overview"))
    board.with_panel(
        headline(
            "Online model servers",
            "Number of online model servers in the selected model groups and roles.",
            f"sum({scoped_group_metric(f'foretoken:model_server_up:sum{{{GROUP}}}')})",
            color=BLUE,
        )
    )
    board.with_panel(
        headline(
            "Input throughput",
            "Input tokens per second for each whole model, across all instances and ranks.",
            model_total("vllm:prompt_tokens_total", rate=True, roles="aggregate|prefill"),
            unit="suffix: token/s",
            interval="5s",
            legend="{{model_name}}",
        )
    )
    board.with_panel(
        headline(
            "Output throughput",
            "Output tokens per second for each whole model, across all instances and ranks.",
            model_total("vllm:generation_tokens_total", rate=True, roles="aggregate|decode"),
            unit="suffix: token/s",
            color=ORANGE,
            interval="5s",
            legend="{{model_name}}",
        )
    )
    board.with_panel(
        headline(
            "Scheduler queued requests",
            "Queued execution requests across all instances, roles and ranks of each model.",
            model_total("vllm:num_requests_waiting"),
            color=None,
            thresholds=steps((None, GREEN), (1, ORANGE)),
            interval="5s",
            legend="{{model_name}}",
        )
    )

    board.with_row(dashboard.Row("Model Serving"))
    # Whole-model totals ignore drill-down filters; backend curves retain those filters.
    # In a disaggregated pipeline, input belongs to prefill and output to decode.
    for title, metric, roles, color in (
        ("Input throughput", "vllm:prompt_tokens_total", "aggregate|prefill", BLUE),
        ("Output throughput", "vllm:generation_tokens_total", "aggregate|decode", ORANGE),
    ):
        backend_rate = model_metric(metric, rate=True, extra=f'inference_foretoken_io_model_role=~"{roles}"')
        board.with_panel(
            series(
                title,
                "Whole-model totals across every instance and rank, with selected backend details. Totals ignore instance, role and rank filters.",
                [
                    foretoken_query(model_total(metric, rate=True, roles=roles), "Total / {{model_name}}"),
                    foretoken_query(
                        f"sum by(namespace,model_group,engine) ({backend_rate})",
                        "{{model_group_display}} / rank {{engine}}",
                    ),
                ],
                unit="suffix: token/s",
                span=12,
            ).override_by_query("A", [
                dashboard_models.DynamicConfigValue(id_val="custom.lineWidth", value=4),
                dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": color}),
            ])
        )
    board.with_panel(
        series(
            "Completed request rate",
            "Completed generation requests per second, with backend details.",
            [
                foretoken_query(model_total("vllm:request_success_total", rate=True, roles="aggregate|decode"), "Total / {{model_name}}"),
                foretoken_query(
                    f"sum by(namespace,model_name,model_group,model_role,engine,finished_reason) ({model_metric('vllm:request_success_total', rate=True)})",
                    "{{model_group_display}} / rank {{engine}} / {{finished_reason}}",
                )
            ],
            unit="reqps",
            span=12,
        )
    )
    board.with_panel(
        series(
            "Scheduler state",
            "Whole-model running and queued execution totals across all roles, with selected backend details.",
            [
                foretoken_query(model_total("vllm:num_requests_running"), "Running total / {{model_name}}"),
                foretoken_query(model_total("vllm:num_requests_waiting"), "Waiting total / {{model_name}}"),
                foretoken_query(f"sum by(namespace,model_name,model_group,model_role,engine) ({model_metric('vllm:num_requests_running')})", "Running / {{model_group_display}} / rank {{engine}}"),
                foretoken_query(f"sum by(namespace,model_name,model_group,model_role,engine) ({model_metric('vllm:num_requests_waiting')})", "Waiting / {{model_group_display}} / rank {{engine}}"),
            ],
            unit="short",
            span=12,
        )
    )
    board.with_row(dashboard.Row("Generation latency"))
    board.with_panel(
        latency(
            model_metric("vllm:e2e_request_latency_seconds_bucket", rate=True, whole_model=True, extra='inference_foretoken_io_model_role=~"aggregate|decode"'),
            "End-to-end latency (E2EL)",
            "Whole-model latency from Frontend processing to generation completion, in seconds; aggregate and decode requests are combined.",
            unit="suffix: s",
        )
    )
    board.with_panel(
        latency(
            model_metric("vllm:time_to_first_token_seconds_bucket", rate=True, whole_model=True, extra='inference_foretoken_io_model_role=~"aggregate|decode"'),
            "Time to first token (TTFT)",
            "Whole-model time from Frontend processing to the first output token, in seconds; aggregate and decode requests are combined.",
            unit="suffix: s",
        )
    )
    board.with_panel(
        latency(
            model_metric("vllm:request_time_per_output_token_seconds_bucket", rate=True, whole_model=True, extra='inference_foretoken_io_model_role=~"aggregate|decode"'),
            "Time per output token (TPOT)",
            "Whole-model time per output token, in milliseconds; each request contributes its average interval.",
            unit="suffix: ms",
            scale=1_000,
            mean_rates=(
                model_metric("vllm:request_time_per_output_token_seconds_sum", rate=True, whole_model=True, extra='inference_foretoken_io_model_role=~"aggregate|decode"'),
                model_metric("vllm:request_time_per_output_token_seconds_count", rate=True, whole_model=True, extra='inference_foretoken_io_model_role=~"aggregate|decode"'),
            ),
        )
    )
    board.with_panel(
        latency(
            model_metric("vllm:inter_token_latency_seconds_bucket", rate=True, whole_model=True, extra='inference_foretoken_io_model_role=~"aggregate|decode"'),
            "Inter-token latency (ITL)",
            "Time between output tokens, in milliseconds.",
            unit="suffix: ms",
            scale=1_000,
            mean_rates=(
                model_metric("vllm:inter_token_latency_seconds_sum", rate=True, whole_model=True, extra='inference_foretoken_io_model_role=~"aggregate|decode"'),
                model_metric("vllm:inter_token_latency_seconds_count", rate=True, whole_model=True, extra='inference_foretoken_io_model_role=~"aggregate|decode"'),
            ),
        )
    )
    board.with_panel(
        series(
            "Stage latency (p95)",
            "P95 queue, prefill and decode time for completed requests, in seconds.",
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
            span=12,
            colors=STAGE_COLORS,
        )
    )
    board.with_panel(
        series(
            "Preemption events / s",
            "Whole-model preemption events per second across every engine, with selected backend details.",
            [
                foretoken_query(model_total("vllm:num_preemptions_total", rate=True), "Total / {{model_name}}"),
                foretoken_query(f"sum by(namespace,model_name,model_group,model_role,engine) ({model_metric('vllm:num_preemptions_total', rate=True)})", "{{model_group_display}} / rank {{engine}}"),
            ],
            unit="ops",
            span=12,
        )
    )
    for title, description, observations in (
        (
            "Request latency samples / s",
            "TTFT and E2EL histogram observations per second for each whole model.",
            (("TTFT", "vllm:time_to_first_token_seconds_count"), ("E2EL", "vllm:e2e_request_latency_seconds_count")),
        ),
        (
            "Token interval samples / s",
            "Output-token interval observations per second for each whole model. These count token intervals.",
            (("ITL", "vllm:inter_token_latency_seconds_count"),),
        ),
    ):
        board.with_panel(
            series(
                title,
                description,
                [
                    foretoken_query(model_total(metric, rate=True, roles="aggregate|decode"), f"{name} / {{{{model_name}}}}")
                    for name, metric in observations
                ],
                unit="suffix: samples/s",
                span=12,
            )
        )

    board.with_row(dashboard.Row("Request lengths"))
    board.with_panel(
        distribution(
            "Prompt length",
            "Distribution of prompt tokens per request across selected engines.",
            "vllm:request_prompt_tokens_bucket",
        )
    )
    board.with_panel(
        distribution(
            "Output length",
            "Distribution of generated tokens per request across selected engines.",
            "vllm:request_generation_tokens_bucket",
        )
    )

    board.with_row(dashboard.Row("Speculative decoding"))
    draft_tokens = model_total("vllm:spec_decode_num_draft_tokens_total", rate=True, roles="aggregate|decode")
    accepted_tokens = model_total("vllm:spec_decode_num_accepted_tokens_total", rate=True, roles="aggregate|decode")
    draft_iterations = model_total("vllm:spec_decode_num_drafts_total", rate=True, roles="aggregate|decode")
    board.with_panel(
        series(
            "Draft and accepted tokens / s",
            "Draft and accepted token rates for each whole model.",
            [
                foretoken_query(draft_tokens, "Draft / {{model_name}}"),
                foretoken_query(accepted_tokens, "Accepted / {{model_name}}"),
            ],
            unit="suffix: token/s",
            span=12,
        ).override_by_query("A", [
            dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": BLUE}),
        ]).override_by_query("B", [
            dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": TEAL}),
        ])
    )
    board.with_panel(
        series(
            "Draft acceptance ratio",
            "Accepted draft tokens divided by proposed draft tokens across all engines.",
            [foretoken_query(f"({accepted_tokens}) / (({draft_tokens}) > 0)", "{{model_name}}")],
            unit="percentunit",
            span=12,
        )
    )
    board.with_panel(
        series(
            "Accepted tokens per draft iteration",
            "Accepted draft tokens per draft iteration across all engines; excludes bonus tokens.",
            [foretoken_query(f"({accepted_tokens}) / (({draft_iterations}) > 0)", "{{model_name}}")],
            unit="short",
            span=12,
        )
    )
    model_role_filter = 'inference_foretoken_io_model_role=~"aggregate|decode"'
    board.with_panel(
        series(
            "Acceptance probability by position",
            "Accepted tokens at each zero-based draft position divided by draft iterations across all engines.",
            [foretoken_query(
                f"sum by(model_name,position) ({model_metric('vllm:spec_decode_num_accepted_tokens_per_pos_total', rate=True, whole_model=True, extra=model_role_filter)}) "
                f"/ on(model_name) group_left() (({draft_iterations}) > 0)",
                "Position {{position}} / {{model_name}}",
            )],
            unit="percentunit",
            span=12,
        )
    )

    draft_time = model_total("vllm:spec_decode_draft_duration_seconds_sum", rate=True, roles="aggregate|decode")
    target_time = model_total("vllm:spec_decode_target_forward_duration_seconds_sum", rate=True, roles="aggregate|decode")
    timed_steps = model_total("vllm:spec_decode_draft_duration_seconds_count", rate=True, roles="aggregate|decode")
    stage_time = f"(({draft_time}) + ({target_time}))"
    board.with_panel(
        series(
            "Speculative stage GPU time",
            "Average draft and target-forward time per measured step.",
            [
                foretoken_query(f"({target_time}) / (({timed_steps}) > 0)", "Target forward / {{model_name}}"),
                foretoken_query(f"({draft_time}) / (({timed_steps}) > 0)", "Draft / {{model_name}}"),
            ],
            unit="s",
            span=12,
        ).override_by_query("A", [
            dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": BLUE}),
        ]).override_by_query("B", [
            dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": TEAL}),
        ])
    )
    board.with_panel(
        series(
            "Speculative GPU time shares",
            "Share of measured time spent on drafting and target forward.",
            [
                foretoken_query(f"({target_time}) / (({stage_time}) > 0)", "Target forward share / {{model_name}}"),
                foretoken_query(f"({draft_time}) / (({stage_time}) > 0)", "Draft share / {{model_name}}"),
            ],
            unit="percentunit",
            span=12,
        ).override_by_query("A", [
            dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": BLUE}),
        ]).override_by_query("B", [
            dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": TEAL}),
        ])
    )
    board.with_panel(
        series(
            "Timed speculative steps / s",
            "Measured speculative decoding steps per second.",
            [foretoken_query(timed_steps, "{{model_name}}")],
            unit="ops",
            span=24,
        )
    )

    board.with_row(dashboard.Row("Cache"))
    board.with_panel(
        series(
            "KV Cache utilization",
            "KV-cache occupancy by model instance and engine rank.",
            [query(f"max by(namespace,model_name,model_group,model_role,engine) ({model_metric('vllm:kv_cache_usage_perc')})", "{{model_group_display}} / rank {{engine}}")],
            unit="percentunit",
            span=12,
        )
    )
    board.with_panel(
        series(
            "Prefix Cache hit ratio",
            "Whole-model cache hits divided by queried tokens. Local and external caches are separate.",
            [
                foretoken_query(
                    model_rate_ratio("vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total"),
                    "{{model_name}} / local",
                ),
                foretoken_query(
                    model_rate_ratio(
                        "vllm:external_prefix_cache_hits_total",
                        "vllm:external_prefix_cache_queries_total",
                    ),
                    "{{model_name}} / external",
                ),
            ],
            unit="percentunit",
            span=12,
            colors={"local": BLUE, "external": TEAL},
        )
    )
    board.with_panel(
        series(
            "Runtime cache filesystem usage",
            "Highest RuntimeCache filesystem usage by model instance.",
            [query(scoped_group_metric(f"foretoken:model_server_runtime_cache_usage_ratio:max{{{GROUP}}}"), "{{model_group_display}}")],
            unit="percentunit",
            span=12,
        )
    )
    board.with_panel(
        series(
            "Runtime cache filesystem free space",
            "Lowest available RuntimeCache filesystem space by model instance.",
            [
                query(
                    scoped_group_metric(f"foretoken:model_server_runtime_cache_available_bytes:min{{{GROUP}}}"),
                    "{{model_group_display}}",
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
            "Utilization of each GPU used by the selected model.",
            "foretoken:accelerator_gpu_utilization_ratio",
            unit="percentunit",
        )
    )
    board.with_panel(
        by_device(
            "GPU memory by device",
            "Memory utilization of each GPU used by the selected model.",
            "foretoken:accelerator_gpu_memory_usage_ratio",
            unit="percentunit",
        )
    )
    board.with_panel(
        by_device(
            "GPU power by device",
            "Power draw of each GPU used by the selected model, in watts.",
            "foretoken:accelerator_gpu_power_watts",
            unit="watt",
        )
    )
    board.with_panel(
        by_device(
            "GPU temperature by device",
            "Temperature reported by each GPU used by the selected model; MetaX uses the chip hotspot sensor.",
            "foretoken:accelerator_gpu_temperature_celsius",
            unit="celsius",
        )
    )

    container = 'namespace=~"$namespace",container="model-server"'
    # Group membership includes workers that do not expose the head's engine metrics.
    selected_pods = (
        f"max by(namespace,pod,model_group,node) (foretoken:accelerator_workload_labels{{{GROUP}}} "
        f"and on(namespace,model_group) ({selected_groups()}))"
    )
    model_pods = (
        'max by(namespace,pod,model_group) (foretoken:accelerator_workload_labels{namespace=~"$namespace"}) '
        '* on(namespace,model_group) group_left(model_name) '
        '(max by(namespace,model_group,model_name) (foretoken:model_instance_info{namespace=~"$namespace",model_name=~"$model_name"}))'
    )
    board.with_panel(
        series(
            "Serving CPU usage",
            "CPU cores used by all model-server Pods of each model, with selected instance and node details.",
            [
                query(
                    f"sum by(model_name) ((sum by(namespace,pod) (rate(container_cpu_usage_seconds_total{{{container}}}[$__rate_interval]))) * on(namespace,pod) group_left(model_name) ({model_pods}))",
                    "Total / {{model_name}}",
                    interval="30s",
                ),
                query(
                    f"sum by(namespace,pod) (rate(container_cpu_usage_seconds_total{{{container}}}[$__rate_interval])) * on(namespace,pod) group_left(model_group,node) ({selected_pods})",
                    "{{model_group_display}} / {{node}}",
                    interval="30s",
                )
            ],
            unit="cores",
            span=12,
        )
    )
    board.with_panel(
        series(
            "Serving memory usage",
            "Working-set memory of all model-server Pods of each model, with selected instance and node details.",
            [
                query(
                    f"sum by(model_name) ((sum by(namespace,pod) (container_memory_working_set_bytes{{{container}}})) * on(namespace,pod) group_left(model_name) ({model_pods}))",
                    "Total / {{model_name}}",
                    interval="30s",
                ),
                query(
                    f"sum by(namespace,pod) (container_memory_working_set_bytes{{{container}}}) * on(namespace,pod) group_left(model_group,node) ({selected_pods})",
                    "{{model_group_display}} / {{node}}",
                    interval="30s",
                )
            ],
            unit="bytes",
            span=12,
        )
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
            "Routing share by backend within each model and execution role.",
            [foretoken_query(
                f"({shares}) and on(namespace,model_group,data_parallel_rank) ({selected_ranks})",
                "{{model_group_display}} / rank {{data_parallel_rank}}",
            )],
            unit="percentunit", span=12,
        )
    )
    board.with_panel(
        series(
            "Routing outcomes",
            "Routing selection rate by stage and outcome within the selected time range.",
            [
                foretoken_query(
                    f"sum by(model_name,round,outcome) (rate(foretoken_router_selections_total{{{ROUTER}}}[$__rate_interval])) "
                    f"and on(model_name,round,outcome) (sum by(model_name,round,outcome) "
                    f"(increase(foretoken_router_selections_total{{{ROUTER}}}[$__range] @ end())) > 0)",
                    "{{model_name}} / {{round}} / {{outcome}}",
                )
            ],
            unit="reqps",
            span=12,
        )
    )
    board.with_panel(
        series(
            "Routing stage latency (p99)",
            "P99 filter, scorer, and picker time across selected Frontend replicas.",
            [
                foretoken_query(
                    "histogram_quantile(0.99, sum by(model_name,round,stage,algorithm,le) "
                    f"(rate(foretoken_router_stage_duration_seconds_bucket{{{ROUTER}}}[$__rate_interval]))) >= 0",
                    "{{model_name}} / {{round}} / {{stage}} / {{algorithm}}",
                )
            ],
            unit="s",
            span=12,
        )
    )
    board.with_panel(
        series(
            "Eligible instances and ranks",
            "Mean available, filtered, and selectable candidate counts per routing selection; selectable includes data-parallel ranks.",
            [
                foretoken_query(
                    f"sum by(model_name,round,stage) (rate(foretoken_router_candidates_sum{{{ROUTER}}}[$__rate_interval])) "
                    f"/ (sum by(model_name,round,stage) (rate(foretoken_router_candidates_count{{{ROUTER}}}[$__rate_interval])) > 0)",
                    "{{model_name}} / {{round}} / {{stage}}",
                )
            ],
            unit="short",
            span=12,
        )
    )
    board.with_row(dashboard.Row("Shared frontend"))
    board.with_panel(
        headline(
            "Online frontend replicas",
            "Number of successfully scraped frontend replicas in the selected services.",
            f"sum(foretoken:frontend_up:sum{{{FRONTEND}}})",
            color=BLUE,
        ).span(12)
    )
    board.with_panel(
        headline(
            "Frontend response starts / s",
            "Frontend responses started per second over the selected rate window.",
            f"sum({frontend_request_rates})",
            unit="reqps",
            interval="5s",
        ).span(12)
    )
    board.with_panel(
        series(
            "Frontend responses by HTTP status",
            "Frontend response starts grouped by HTTP status class.",
            [foretoken_query(f"sum by(status) ({frontend_request_rates})", "{{status}}")],
            unit="reqps",
            span=12,
            colors={"2xx": GREEN, "4xx": ORANGE, "5xx": RED},
        )
    )
    board.with_panel(
        series(
            "Frontend responses by endpoint",
            "Frontend response starts grouped by HTTP endpoint.",
            [foretoken_query(f"sum by(handler) ({frontend_request_rates})", "{{handler}}")],
            unit="reqps",
            span=12,
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
            "Time to HTTP response headers, in seconds.",
            unit="suffix: s",
            span=8,
            dimensions="",
        )
    )
    board.with_panel(
        series(
            "Model preparation and dispatch wait",
            "Requests waiting for runtime preparation or backend dispatch, grouped by scaling-target kind.",
            [
                query(
                    f"sum by(target_kind) (foretoken:frontend_upstream_queued_requests:sum{{{FRONTEND}}})",
                    "{{target_kind}}",
                )
            ],
            unit="short",
            span=8,
            colors={"Pool": BLUE},
        )
    )

    board.with_panel(
        series(
            "Frontend cache-index health",
            "Healthy KV event sources divided by configured sources.",
            [query(f"foretoken:frontend_kv_index_source_health_ratio:min{{{FRONTEND}}}", "{{frontend_service}}")],
            unit="percentunit",
            span=8,
        )
    )
    admission_panels(board)
    control_plane = dashboard.Row("Control plane")
    control_plane.with_panel(
        series(
            "Reconcile errors",
            "Reconciliation errors per second by controller.",
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
            "Reconcile latency (p99)",
            "P99 reconciliation time by controller.",
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
            "Depth of each controller workqueue.",
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
            "Published recommendation, adjusted target, and applied capacity.",
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
            "Ready and routable capacity compared with applied desired replicas.",
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
            "Age in seconds since the latest observation and evaluation.",
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
            "Latest trigger, decision, and adjustment outcomes for the selected model service."
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

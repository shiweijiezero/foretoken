# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Builds the Foretoken System Overview dashboard shipped by the Helm chart.

Run `make dashboard` from the repository root after changing this file. It writes
`deploy/charts/foretoken/files/grafana/foretoken-system-overview.json`, which the chart
installs as a Grafana ConfigMap. The generated JSON is the deployed artifact; this
module is the source to edit.

The dashboard reads the recording rules in `deploy/charts/foretoken/files/recording-rules.yaml`
plus the bounded router, controller, and autoscaling metrics. Its section order follows the
Dynamo dashboard: service health first, then the request path from the Frontend through model
serving and caches to accelerators, and finally routing, control-plane, and autoscaling decisions.
"""

from __future__ import annotations

from grafana_foundation_sdk.builders import common, dashboard, prometheus, stat, table, timeseries
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models import common as models
from grafana_foundation_sdk.models import dashboard as dashboard_models
from grafana_foundation_sdk.models import prometheus as prometheus_models

PROMETHEUS = dashboard_models.DataSourceRef(type_val="prometheus", uid="${DS_PROMETHEUS}")

GREEN = "#0ca30c"
AMBER = "#eda100"
RED = "#d03b3b"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
TEAL = "#1baf7a"
QUANTILE_COLORS = {"p50": "#86b6ef", "p90": "#3987e5", "p99": "#184f95"}

# Label selectors shared by the dashboard variables and the queries below.
FRONTEND = 'namespace=~"$namespace",frontend_service=~"$frontend_service"'
GROUP = 'namespace=~"$namespace",model_group=~"$model_group",model_role=~"$model_role"'
MODEL = GROUP + ',model_name=~"$model_name"'
ROUTER = 'namespace=~"$namespace",inference_foretoken_io_frontend_service=~"$frontend_service"'
SERVICE = 'namespace=~"$namespace",modelservice=~"$model_service"'
CONTROLLER = 'job="foretoken-control-plane"'
AUTOSCALING_TARGET = "namespace,modelservice,target_kind,target_name,role"
AUTOSCALING_LEGEND = "{{modelservice}} / {{target_name}} / {{role}}"


def query(expr: str, legend: str | None = None) -> prometheus.Dataquery:
    target = prometheus.Dataquery().datasource(PROMETHEUS).expr(expr).range()
    if legend is not None:
        target.legend_format(legend)
    return target


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
        .with_target(query(expr, title).ref_id("A"))
        .span(3)
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
) -> timeseries.Panel:
    """A time series panel; `colors` pins legend names to fixed colors."""
    panel = (
        timeseries.Panel()
        .title(title)
        .description(description)
        .datasource(PROMETHEUS)
        .unit(unit)
        .color_scheme(dashboard.FieldColor().mode(dashboard_models.FieldColorModeId.PALETTE_CLASSIC_BY_NAME))
        .line_width(2)
        .fill_opacity(8)
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
    for name, color in (colors or {}).items():
        panel.override_by_name(
            name,
            [dashboard_models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": color})],
        )
    return panel


def quantiles(rule: str, selector: str, title: str, description: str) -> timeseries.Panel:
    return series(
        title,
        description,
        [query(f"max by(quantile) ({rule}{{{selector}}})", "{{quantile}}")],
        unit="s",
        span=8,
        colors=QUANTILE_COLORS,
    )


def autoscaling(
    title: str, description: str, metrics: dict[str, str], *, unit: str, age: bool = False
) -> timeseries.Panel:
    """One line per published autoscaling target for each metric (legend suffix -> metric name).

    With `age`, the metrics are Unix timestamps and the panel shows how long ago they were set.
    """
    prefix = "time() - " if age else ""
    targets = [
        query(f"{prefix}max by({AUTOSCALING_TARGET}) ({metric}{{{SERVICE}}})", f"{AUTOSCALING_LEGEND} / {suffix}")
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


def build() -> dashboard_models.Dashboard:
    board = (
        dashboard.Dashboard("Foretoken System Overview")
        .uid("foretoken-system-overview")
        .tags(["foretoken", "inference", "operations"])
        .editable()
        .tooltip(dashboard_models.DashboardCursorSync.CROSSHAIR)
        .refresh("10s")
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

    board.with_row(dashboard.Row("Overview"))
    board.with_panel(
        headline(
            "Frontend targets",
            "Prometheus targets currently reporting for the selected Frontend services.",
            f"sum(foretoken:frontend_up:sum{{{FRONTEND}}})",
            color=GREEN,
            thresholds=steps((None, RED), (1, GREEN)),
        )
    )
    board.with_panel(
        headline(
            "Model servers",
            "Prometheus targets currently reporting for the selected model groups and roles.",
            f"sum(foretoken:model_server_up:sum{{{GROUP}}})",
            color=GREEN,
            thresholds=steps((None, RED), (1, GREEN)),
        )
    )
    board.with_panel(
        headline(
            "Requests / s",
            "Frontend responses started per second over five minutes.",
            f"sum(foretoken:frontend_http_response_starts:rate5m{{{FRONTEND}}})",
            unit="reqps",
        )
    )
    board.with_panel(
        headline(
            "5xx ratio",
            "HTTP responses that started with 5xx divided by all started responses. "
            "Streaming failures after headers are not included.",
            f"(sum(foretoken:frontend_http_response_starts:rate5m{{{FRONTEND},status=\"5xx\"}}) "
            f"or 0 * sum(foretoken:frontend_http_response_starts:rate5m{{{FRONTEND}}})) "
            f"/ clamp_min(sum(foretoken:frontend_http_response_starts:rate5m{{{FRONTEND}}}), 1e-9)",
            unit="percentunit",
            color=GREEN,
            thresholds=steps((None, GREEN), (0.01, AMBER), (0.05, RED)),
        )
    )
    board.with_panel(
        headline(
            "Prompt tokens / s",
            "Prompt tokens processed per second by the selected model servers.",
            f"sum(foretoken:model_server_prompt_tokens:rate5m{{{MODEL}}})",
            unit="suffix: tok/s",
        )
    )
    board.with_panel(
        headline(
            "Output tokens / s",
            "Generated tokens produced per second by the selected model servers.",
            f"sum(foretoken:model_server_generation_tokens:rate5m{{{MODEL}}})",
            unit="suffix: tok/s",
            color=ORANGE,
        )
    )
    board.with_panel(
        headline(
            "Queued requests",
            "Requests waiting for frontend admission to a scaling target.",
            f"sum(foretoken:frontend_upstream_queued_requests:sum{{{FRONTEND}}})",
            color=ORANGE,
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
            [query(f"sum by(status) (foretoken:frontend_http_response_starts:rate5m{{{FRONTEND}}})", "{{status}}")],
            unit="reqps",
            span=6,
            colors={"2xx": GREEN, "4xx": ORANGE, "5xx": RED},
        )
    )
    board.with_panel(
        series(
            "Frontend response-start latency",
            "Time until the Frontend handler produces HTTP response headers. "
            "This excludes SSE body delivery; it is neither TTFT nor full-stream duration.",
            [
                query(
                    "max by(quantile) (foretoken:frontend_http_response_start_latency_seconds:quantile5m"
                    f'{{{FRONTEND},handler=~"/v1/(chat/completions|completions|generate)"}})',
                    "{{quantile}}",
                )
            ],
            unit="s",
            span=6,
            colors=QUANTILE_COLORS,
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
    board.with_panel(
        series(
            "KV index source health",
            "Healthy KV event sources divided by configured sources. Disabled or unavailable indexing reports zero.",
            [query(f"foretoken:frontend_kv_index_source_health_ratio:min{{{FRONTEND}}}", "{{frontend_service}}")],
            unit="percentunit",
            span=6,
        )
    )

    board.with_row(dashboard.Row("Model Serving"))
    board.with_panel(
        series(
            "Completed request rate",
            "Successfully completed model-server requests grouped by finish reason.",
            [
                query(
                    f"sum by(finished_reason) (foretoken:model_server_completed_requests:rate5m{{{MODEL}}})",
                    "{{finished_reason}}",
                )
            ],
            unit="reqps",
            span=8,
        )
    )
    board.with_panel(
        series(
            "Token throughput",
            "Prompt and generated token throughput for the selected model servers.",
            [
                query(f"sum(foretoken:model_server_prompt_tokens:rate5m{{{MODEL}}})", "Prompt"),
                query(f"sum(foretoken:model_server_generation_tokens:rate5m{{{MODEL}}})", "Output"),
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
                query(f"sum(foretoken:model_server_requests_running:sum{{{MODEL}}})", "Running"),
                query(f"sum(foretoken:model_server_requests_waiting:sum{{{MODEL}}})", "Waiting"),
            ],
            unit="short",
            span=8,
            colors={"Running": BLUE, "Waiting": ORANGE},
        )
    )
    board.with_panel(
        quantiles(
            "foretoken:model_server_e2e_request_latency_seconds:quantile5m",
            MODEL,
            "Generation completion latency",
            "Maximum per-model-group quantile from the shared Frontend arrival time to the model-server "
            "terminal output. Does not measure downstream client body consumption.",
        )
    )
    board.with_panel(
        quantiles(
            "foretoken:model_server_time_to_first_token_seconds:quantile5m",
            MODEL,
            "Time to first token",
            "Maximum per-model-group TTFT quantile, using the same Frontend arrival-time boundary "
            "for chat and completion requests.",
        )
    )
    board.with_panel(
        quantiles(
            "foretoken:model_server_time_per_output_token_seconds:quantile5m",
            MODEL,
            "Time per output token",
            "Maximum per-model-group output-token latency quantile. This is not a cluster-wide quantile.",
        )
    )

    board.with_row(dashboard.Row("Cache"))
    board.with_panel(
        series(
            "KV Cache utilization",
            "Highest in-engine KV-cache utilization grouped by model role.",
            [query(f"max by(model_role) (foretoken:model_server_kv_cache_usage_ratio:max{{{MODEL}}})", "{{model_role}}")],
            unit="percentunit",
            span=6,
        )
    )
    board.with_panel(
        series(
            "Prefix Cache hit ratio",
            "Average local and external Prefix Cache token hit ratios across selected model groups.",
            [query(f"avg by(cache) (foretoken:model_server_prefix_cache_hit_ratio:rate5m{{{MODEL}}})", "{{cache}}")],
            unit="percentunit",
            span=6,
            colors={"local": BLUE, "external": TEAL},
        )
    )
    board.with_panel(
        series(
            "RuntimeCache utilization",
            "Mounted RuntimeCache filesystem utilization. Series are absent for model groups without a RuntimeCache.",
            [query(f"foretoken:model_server_runtime_cache_usage_ratio:max{{{GROUP}}}", "{{model_group}} / {{model_role}}")],
            unit="percentunit",
            span=6,
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
            span=6,
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
    container = 'namespace=~"$namespace",container=~"frontend|model-server"'
    board.with_panel(
        series(
            "Serving CPU usage",
            "CPU cores consumed by Frontend and model-server containers in the selected namespaces.",
            [
                query(
                    f"sum by(container) (rate(container_cpu_usage_seconds_total{{{container}}}[$__rate_interval]))",
                    "{{container}}",
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
            [query(f"sum by(container) (container_memory_working_set_bytes{{{container}}})", "{{container}}")],
            unit="bytes",
            span=6,
            colors={"frontend": BLUE, "model-server": ORANGE},
        )
    )

    board.with_row(dashboard.Row("Routing decisions"))
    board.with_panel(
        series(
            "Routing outcomes",
            "Selection results by workflow round. A selection failure is not an HTTP status; "
            "a request can involve several rounds.",
            [
                query(
                    f"sum by(round,outcome) (rate(foretoken_router_selections_total{{{ROUTER}}}[$__rate_interval]))",
                    "{{round}} / {{outcome}}",
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
                query(
                    "histogram_quantile(0.99, sum by(round,stage,algorithm,le) "
                    f"(rate(foretoken_router_stage_duration_seconds_bucket{{{ROUTER}}}[$__rate_interval])))",
                    "{{round}} / {{stage}} / {{algorithm}}",
                )
            ],
            unit="s",
            span=8,
        )
    )
    board.with_panel(
        series(
            "Routing candidates",
            "Mean available, filtered and selectable candidate counts. Selectable counts include data-parallel ranks.",
            [
                query(
                    f"sum by(round,stage) (rate(foretoken_router_candidates_sum{{{ROUTER}}}[$__rate_interval])) "
                    f"/ sum by(round,stage) (rate(foretoken_router_candidates_count{{{ROUTER}}}[$__rate_interval]))",
                    "{{round}} / {{stage}}",
                )
            ],
            unit="short",
            span=8,
        )
    )

    board.with_row(dashboard.Row("Control plane"))
    board.with_panel(
        series(
            "Reconcile errors",
            "Controller-runtime errors per second. This section observes the platform controller "
            "independently of workload namespace filters.",
            [
                query(
                    f"sum by(controller) (rate(controller_runtime_reconcile_errors_total{{{CONTROLLER}}}[$__rate_interval]))",
                    "{{controller}}",
                )
            ],
            unit="ops",
            span=8,
        )
    )
    board.with_panel(
        series(
            "Reconcile latency",
            "P99 reconciliation time by controller; this is control-plane work, not inference request latency.",
            [
                query(
                    "histogram_quantile(0.99,sum by(controller,le) "
                    f"(rate(controller_runtime_reconcile_time_seconds_bucket{{{CONTROLLER}}}[$__rate_interval])))",
                    "{{controller}}",
                )
            ],
            unit="s",
            span=8,
        )
    )
    board.with_panel(
        series(
            "Controller workqueues",
            "Queued reconciliations. Current replica counts are deduplicated rather than added "
            "across controller replicas.",
            [query(f"max by(name) (workqueue_depth{{{CONTROLLER}}})", "{{name}}")],
            unit="short",
            span=8,
        )
    )

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
            .expr(
                f"max by({AUTOSCALING_TARGET},stage,algorithm,disposition,reason) "
                f"(foretoken_autoscaling_stage{{{SERVICE}}})"
            )
            .format(prometheus_models.PromQueryFormat.TABLE)
            .instant()
            .ref_id("A")
        )
        .span(24)
        .height(9)
    )
    return board.build()


if __name__ == "__main__":
    print(JSONEncoder(sort_keys=True, indent=2).encode(build()))

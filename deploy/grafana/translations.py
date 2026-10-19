# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Translate Grafana display text while preserving dashboard queries and identities."""

# Exact UI-string translations keep both dashboards on one query and layout definition.
ZH = {
    "Foretoken System Overview": "Foretoken 系统概览",
    "Overview": "概览",
    "Reading this dashboard": "看板读法",
    "Select a model for inference metrics, and a model and frontend for admission; HTTP traffic covers the whole frontend. Waiting and unfinished occupancy are shared within each frontend service and model; replica observations are deduplicated. Adjust the time range to inspect trends.":
        "选择模型查看推理指标，选择模型和前端查看准入；HTTP 流量覆盖整个前端。等待和未完成占用由同一前端服务的各副本共享，看板对重复观测去重。调整时间范围查看趋势。",
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
    "Catalog telemetry complete": "模型目录指标完整",
    "1 means the published model count matches inventory; an empty catalog is valid. 0 means missing reporting or a failed scrape.":
        "1 表示发布的模型数与目录指标一致，空目录也有效；0 表示目录指标缺失或抓取失败。",
    "Admission results and wait": "准入结果与等待",
    "Admission resources": "准入资源",
    "Admission calls / s": "准入调用速率",
    "HTTP model-admission attempts per second; batches count once.": "各模型每秒发起的 HTTP 准入调用数，批次计一次。",
    "Dispatched calls / s": "派发成功速率",
    "HTTP requests reaching first backend acceptance per second; batches count once.": "每秒首次获后端接受的 HTTP 请求数，批次计一次。",
    "Enqueued calls / s": "入队速率",
    "HTTP requests acquiring waiting ownership per second; batches count once.": "每秒取得等待名额的 HTTP 请求数，批次计一次。",
    "Capacity rejection ratio": "容量拒绝比例",
    "Share of each model's completed HTTP admission calls rejected for capacity.": "各模型已结束的 HTTP 准入调用中，因容量不足被拒绝的占比。",
    "Admission timeout ratio": "准入超时比例",
    "Share of each model's completed HTTP admission calls that timed out.": "各模型已结束的 HTTP 准入调用中，超时调用的占比。",
    "Dispatch wait (p95)": "派发等待（P95）",
    "Waiting-ownership p95 through first backend acceptance, including preparation.": "从取得等待名额到首次后端接受的 P95，包括输入准备。",
    "No samples": "无样本",
    "Admission by replica": "各模型与副本准入状态",
    "Compare bounded admission, ledger availability and request results across frontend replicas.": "对比各前端副本的有界准入配置、共享账本可用性与请求结果。",
    "Admission calls and results / s": "准入到达与结果速率",
    "Results distinguish backend acceptance (dispatched) from CPU-only completion (prepared); arrivals include pending calls.": "结果区分后端接受（dispatched）与仅 CPU 工作完成（prepared）；到达包含尚未结束的调用。",
    "Waiting duration by result": "各结果的排队等待",
    "Compare waiting duration for dispatched, prepared, timed-out and cancelled calls.": "对比后端接受、CPU 工作完成、超时和取消调用的等待时间。",
    "Waiting exit samples / s": "排队退出样本速率",
    "Completed waiting-ownership observations per second, grouped by model, origin and result.": "按模型、来源与结果展示每秒结束的排队等待样本数。",
    "Waiting work units": "等待工作单位",
    "Reserved and accepted work units": "预留与已接受工作单位",
    "Waiting limit": "等待上限",
    "Bounded admission": "有界准入",
    "Store available": "账本可用",
    "Ledger observations by replica": "各副本的共享账本观测",
    "Replicas observe the same shared occupancy. Missing counts indicate a failed ledger read, not zero usage.": "各副本观测同一份共享占用。缺失数值表示账本读取失败，不表示占用为零。",
    "Shared dispatch reservations plus accepted unfinished generation units. There is no model-wide concurrency limit.": "共享派发预留及后端已接受但尚未完成的生成单位；不存在模型总并发上限。",
    "Shared waiting occupancy includes preparation and unresolved submission. The limit is shared by the service's replicas.": "共享等待占用包括输入准备和接受结果未定的提交；服务各副本共同使用等待上限。",
    "Replica": "副本",
    "Scrape": "抓取",
    "Telemetry complete": "指标完整",
    "Dispatch wait p95": "派发等待 P95",
    "{{namespace}} / {{frontend_service}} / {{model_name}} / {{origin}} / arrivals": "{{namespace}} / {{frontend_service}} / {{model_name}} / {{origin}} / 到达",
    "Occupancy / {{namespace}} / {{frontend_service}} / {{model_name}} / {{frontend_service_uid}}": "占用 / {{namespace}} / {{frontend_service}} / {{model_name}} / {{frontend_service_uid}}",
    "Limit / {{namespace}} / {{frontend_service}} / {{model_name}} / {{frontend_service_uid}}": "上限 / {{namespace}} / {{frontend_service}} / {{model_name}} / {{frontend_service_uid}}",
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

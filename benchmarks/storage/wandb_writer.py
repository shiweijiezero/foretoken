from __future__ import annotations

import os
from typing import Any, Optional

from benchmarks.metrics.aggregator import (
    attach_user_throughput,
    tokens_per_s_per_user,
)

# Extra W&B key (EvalScope BenchmarkMetrics has no per-user field).
TOK_S_PER_USER_KEY = "Output Throughput per User (tok/s)"


def metrics_to_evalscope_message(
    metrics: dict[str, Any],
    *,
    parallel: Optional[int] = None,
    request_rate: Optional[float] = None,
    api_type: Optional[str] = None,
) -> dict[str, Any]:
    """Fill ``BenchmarkMetrics`` from foretoken aggregates → ``create_message()``.

    Returned dict is logged to W&B; EvalScope keys unchanged, plus
    ``Output Throughput per User (tok/s)``.
    """
    from benchmarks.deps.evalscope import require_benchmark_metrics

    BenchmarkMetrics = require_benchmark_metrics()

    latency = metrics.get("latency") or {}
    ttft = metrics.get("ttft") or {}
    tpot = metrics.get("tpot") or {}
    itl = metrics.get("itl") or tpot
    throughput = metrics.get("throughput") or {}

    conc = parallel
    if conc is None and metrics.get("parallel") is not None:
        conc = int(metrics["parallel"])
    if conc is None and metrics.get("concurrency") is not None:
        conc = int(metrics["concurrency"])

    if request_rate is not None:
        rate = float(request_rate)
    elif metrics.get("rate") is not None:
        rate = float(metrics["rate"])
    else:
        rate = -1.0

    def _opt(key: str, default: float = -1.0) -> float:
        if key not in metrics or metrics[key] is None:
            return default
        return float(metrics[key])

    turns = _opt("avg_turns_per_request", -1.0)
    if turns <= 0:
        turns = -1.0

    in_tok_s = float(throughput.get("input_token/s") or 0.0)
    out_tok_s = float(
        throughput.get("output_token/s")
        or throughput.get("token/s")
        or 0.0
    )
    total_tok_s = float(
        throughput.get("total_token/s") or (in_tok_s + out_tok_s)
    )

    message = BenchmarkMetrics(
        concurrency=int(conc or 0),
        rate=rate,
        total_requests=int(metrics.get("request_num") or 0),
        succeed_requests=int(metrics.get("success_num") or 0),
        failed_requests=int(metrics.get("failed_num") or 0),
        total_time=float(metrics.get("benchmark_time") or 0.0),
        avg_latency=float(latency.get("mean") or 0.0),
        avg_first_chunk_latency=float(ttft.get("mean") or 0.0),
        avg_time_per_output_token=float(tpot.get("mean") or 0.0),
        avg_inter_token_latency=float(itl.get("mean") or 0.0),
        qps=float(throughput.get("request/s") or 0.0),
        avg_prompt_tokens=float(metrics.get("avg_input_tokens") or 0.0),
        avg_completion_tokens=float(metrics.get("avg_output_tokens") or 0.0),
        avg_input_token_throughput=in_tok_s,
        avg_output_token_throughput=out_tok_s,
        avg_total_token_throughput=total_tok_s,
        avg_turns_per_request=turns,
        avg_cached_percent=_opt("avg_cached_percent", -1.0),
        avg_first_turn_ttft=_opt("avg_first_turn_ttft", -1.0),
        avg_subsequent_turn_ttft=_opt("avg_subsequent_turn_ttft", -1.0),
    ).create_message(api_type=api_type)

    tok_user = throughput.get("token/s/user")
    if tok_user is None:
        tok_user = tokens_per_s_per_user(out_tok_s, conc)
    message[TOK_S_PER_USER_KEY] = round(float(tok_user), 4)
    return message


def _as_list(value: Any, default: list[Any]) -> list[Any]:
    if value is None:
        return list(default)
    if isinstance(value, list):
        return list(value)
    return [value]


def _pad_to_len(values: list[Any], length: int) -> list[Any]:
    if not values:
        values = [None]
    if len(values) == 1 and length > 1:
        return [values[0]] * length
    if len(values) >= length:
        return values[:length]
    return values + [values[-1]] * (length - len(values))


def _evalscope_load_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    parallel = _as_list(config.get("parallel"), [1])
    number = _as_list(config.get("number"), [100])
    rate = _as_list(config.get("rate"), [-1.0])
    open_loop = bool(config.get("open_loop"))

    if open_loop:
        length = max(len(number), len(rate))
        number = _pad_to_len(number, length)
        rate = _pad_to_len(rate, length)
        parallel = [-1] * length
    else:
        length = max(len(parallel), len(number))
        parallel = _pad_to_len(parallel, length)
        number = _pad_to_len(number, length)
        if len(rate) != 1 and len(rate) != length:
            rate = _pad_to_len(rate, length)

    out: dict[str, Any] = {
        "parallel": parallel,
        "number": number,
        "rate": rate,
        "open_loop": open_loop,
    }
    for key in ("max_tokens", "temperature", "stream", "dataset"):
        if key in config and config[key] is not None:
            out[key] = config[key]
    return out


def build_evalscope_visualizer_args(
    *,
    model: str,
    url: str,
    outputs_dir: str,
    name: str,
    config: Optional[dict[str, Any]] = None,
):
    """EvalScope ``Arguments`` for ``init_visualizer``."""
    from benchmarks.deps.evalscope import require_visualizer_args

    VisualizerType, Arguments = require_visualizer_args()

    kwargs: dict[str, Any] = {
        "model": model or "model",
        "url": url or "http://127.0.0.1:8000/v1/chat/completions",
        "outputs_dir": outputs_dir or ".",
        "name": name or None,
        "visualizer": VisualizerType.WANDB,
    }
    if isinstance(config, dict):
        kwargs.update(_evalscope_load_kwargs(config))
    return Arguments(**kwargs)


def default_wandb_group_name(
    *,
    model: str = "",
    run_name: str = "",
) -> str:
    """Stable W&B group id for list-sweep sibling runs."""
    if run_name.strip():
        return run_name.strip()
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    model_id = (model or "").strip()
    return f"{model_id}_{stamp}" if model_id else stamp


def sweep_point_run_name(
    group: str,
    *,
    parallel: int,
    number: int,
    rate: float,
    open_loop: bool = False,
) -> str:
    """Per-point run name under a shared sweep group."""
    p_tag = "open" if open_loop or int(parallel) < 0 else str(int(parallel))
    rate_tag = "inf" if rate is None or float(rate) <= 0 else f"{float(rate):g}"
    return f"{group}-p{p_tag}-n{int(number)}-r{rate_tag}"


class WandbWriter:
    """Thin wrapper around EvalScope W&B + foretoken ``tok/s/user``.

    Same as ``evalscope perf`` for core keys::

        init_visualizer(args)
        maybe_log_to_visualizer(args, metrics.create_message(...))

    Additionally logs ``Output Throughput per User (tok/s)``.
    List sweep uses one W&B run per point with a shared ``group``.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        project: str = "foretoken-bench",
        entity: str = "",
        run_name: str = "",
        config: Optional[dict[str, Any]] = None,
        wandb_dir: str = "",
        api_type: Optional[str] = None,
        group: str = "",
        job_type: str = "",
    ):
        self.enabled = enabled
        self.api_type = api_type
        self.group = (group or "").strip()
        self.job_type = (job_type or "").strip()
        self._args = None
        self._error: Optional[str] = None
        self._wandb_stack: Optional[dict[str, Any]] = None

        if not enabled:
            return

        try:
            from benchmarks.deps.evalscope import require_wandb_stack

            self._wandb_stack = require_wandb_stack()
        except ImportError as e:
            self._error = str(e)
            print(f"[wandb] disabled: {self._error}")
            self.enabled = False
            return

        if wandb_dir:
            os.makedirs(wandb_dir, exist_ok=True)
            wandb_dir = os.path.abspath(wandb_dir)

        if not run_name:
            from datetime import datetime

            model_id = ""
            if isinstance(config, dict):
                model_id = str(config.get("model") or "").strip()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_name = f"{model_id}_{stamp}" if model_id else stamp

        model = "model"
        url = "http://127.0.0.1:8000/v1/chat/completions"
        if isinstance(config, dict):
            model = str(config.get("model") or model)
            url = str(config.get("url") or url)

        try:
            self._args = build_evalscope_visualizer_args(
                model=model,
                url=url,
                outputs_dir=wandb_dir or ".",
                name=run_name,
                config=config,
            )
            self._call_init_visualizer(
                project=project or "foretoken-bench",
                entity=entity or "",
            )
        except Exception as e:
            self._error = str(e)
            self.enabled = False
            self._args = None
            print(f"[wandb] init failed: {e}")

    def _call_init_visualizer(
        self,
        *,
        project: str,
        entity: str,
    ) -> None:
        """Call EvalScope ``init_visualizer``; override project/entity/group."""
        assert self._wandb_stack is not None
        wandb = self._wandb_stack["wandb"]
        log_utils = self._wandb_stack["log_utils"]
        init_visualizer = self._wandb_stack["init_visualizer"]
        current_time = self._wandb_stack["current_time"]

        group = self.group
        job_type = self.job_type

        # Mirror EvalScope ``init_wandb``, except project/entity/group.
        def init_wandb(args: Any) -> None:
            os.environ["WANDB_SILENT"] = "true"
            os.environ["WANDB_DIR"] = args.outputs_dir
            stamp = current_time().strftime("%Y%m%d_%H%M%S")
            name = args.name if args.name else f"{args.model_id}_{stamp}"
            logging_config = log_utils._get_sanitized_config(args)
            if args.wandb_api_key is not None:
                wandb.login(key=args.wandb_api_key)
            init_kwargs: dict[str, Any] = {
                "project": project,
                "name": name,
                "config": logging_config,
            }
            if entity:
                init_kwargs["entity"] = entity
            if group:
                init_kwargs["group"] = group
            if job_type:
                init_kwargs["job_type"] = job_type
            wandb.init(**init_kwargs)

        orig = log_utils.init_wandb
        log_utils.init_wandb = init_wandb
        try:
            init_visualizer(self._args)
        finally:
            log_utils.init_wandb = orig

        if wandb.run is not None:
            url_s = getattr(wandb.run, "url", None) or ""
            group_s = f" group={group}" if group else ""
            print(f"[wandb] run: {wandb.run.name}{group_s} {url_s}".strip())
            if os.environ.get("WANDB_DIR"):
                print(f"[wandb] local dir: {os.environ['WANDB_DIR']}")

    @property
    def active(self) -> bool:
        if not self.enabled or self._args is None or self._wandb_stack is None:
            return False
        return self._wandb_stack["wandb"].run is not None

    def log_perf(
        self,
        metrics: dict[str, Any],
        *,
        parallel: Optional[int] = None,
        request_rate: Optional[float] = None,
    ) -> None:
        """EvalScope message + ``tok/s/user`` → ``maybe_log_to_visualizer``."""
        if not self.active or self._wandb_stack is None:
            return

        if parallel is not None or "token/s/user" not in (
            metrics.get("throughput") or {}
        ):
            metrics = attach_user_throughput(
                dict(metrics),
                parallel=parallel if parallel is not None else metrics.get("parallel"),
            )

        try:
            message = metrics_to_evalscope_message(
                metrics,
                parallel=parallel,
                request_rate=request_rate,
                api_type=self.api_type,
            )
        except ImportError as e:
            print(f"[wandb] {e}")
            return

        self._wandb_stack["maybe_log_to_visualizer"](self._args, message)

    def finish(self) -> None:
        try:
            if self._wandb_stack is None:
                return
            wandb = self._wandb_stack["wandb"]
            if wandb.run is not None:
                wandb.run.finish()
        except Exception as e:
            print(f"[wandb] finish failed: {e}")

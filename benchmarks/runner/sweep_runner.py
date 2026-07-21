from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from benchmarks.runner.single_runner import SingleRunner
from benchmarks.metrics.aggregator import (
    MetricsAggregator,
    attach_user_throughput,
)

RunWrapper = Callable[
    [Callable[[], Awaitable[dict[str, Any]]]],
    Awaitable[tuple[dict[str, Any], Optional[dict[str, Any]]]],
]

PointStartHook = Callable[[int, int, float], None]
PointEndHook = Callable[[dict[str, Any]], None]


class SweepRunner:
    """Run closed-loop / open-loop benchmarks across (parallel, number, rate)."""

    def __init__(
        self,
        client_cls,
        url: str,
        model: str,
        *,
        points: list[tuple[int, int, float]],
        api_key: str = "",
        timeout: int = 300,
        max_tokens: int = 128,
        temperature: float = 0.0,
        stream: bool = True,
        requests: Optional[list[dict[str, Any]]] = None,
        open_loop: bool = False,
    ):
        self.client_cls = client_cls
        self.url = url
        self.model = model
        self.points = points
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.stream = stream
        self.requests = requests
        self.open_loop = open_loop

    async def run(
        self,
        wrap_run: Optional[RunWrapper] = None,
        on_result_factory: Optional[Callable[..., Optional[Callable]]] = None,
        on_point_start: Optional[PointStartHook] = None,
        on_point_end: Optional[PointEndHook] = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        for parallel, number, rate in self.points:
            parallel, number, rate = int(parallel), int(number), float(rate)
            print("=" * 50)
            print(
                f"Running parallel={parallel} "
                f"number={number} rate={rate}"
            )
            print("=" * 50)

            if on_point_start is not None:
                on_point_start(parallel, number, rate)

            client = self.client_cls(
                self.url,
                self.model,
                timeout=self.timeout,
                api_key=self.api_key,
            )
            on_result = None
            if on_result_factory is not None:
                on_result = on_result_factory(parallel, number, rate)

            runner = SingleRunner(
                client,
                self.requests,
                parallel,
                number=number,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                stream=self.stream,
                on_result=on_result,
                rate=rate,
                open_loop=self.open_loop,
            )

            engine: Optional[dict[str, Any]] = None
            if wrap_run is not None:
                output, engine = await wrap_run(runner.run)
            else:
                output = await runner.run()

            metrics = MetricsAggregator().aggregate(output)
            resolved_parallel = -1 if self.open_loop else parallel
            metrics["number"] = number
            metrics["rate"] = rate
            attach_user_throughput(metrics, parallel=resolved_parallel)
            if engine is not None:
                metrics["engine"] = engine.get("summary")
                metrics["_engine_timeseries"] = engine.get("timeseries") or []
            if on_point_end is not None:
                on_point_end(metrics)
            results.append(metrics)

        return results

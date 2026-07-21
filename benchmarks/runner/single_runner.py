from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from tqdm.asyncio import tqdm as tqdm_asyncio

# Optional sync callback: on_result(partial_output) after each completed request.
OnResultCallback = Callable[[dict[str, Any]], None]

_SENTINEL = object()


def _scalarize_args(args) -> None:
    """Match ``evalscope.perf.main.run_one_benchmark`` list → scalar."""
    if isinstance(args.parallel, list):
        args.parallel = args.parallel[0]
    if isinstance(args.number, list):
        args.number = args.number[0]
    if isinstance(args.rate, list):
        args.rate = args.rate[0]


class _VLLMStrategyClient:
    """Adapter so EvalScope strategies can call ``client.post(request)``."""

    def __init__(self, runner: "SingleRunner", benchmark_data_cls: Any):
        self._runner = runner
        self._BenchmarkData = benchmark_data_cls

    async def post(self, request: Any):
        result = await self._runner._dispatch(request)
        return self._runner._to_benchmark_data(result, self._BenchmarkData)


class SingleRunner:
    """Dispatch via EvalScope ``ClosedLoopStrategy`` / ``OpenLoopStrategy``.

    Semantics (same as EvalScope):
    - Default closed-loop: semaphore = ``parallel``.
    - ``rate != -1``: Poisson absolute-time pacing (still closed-loop unless
      ``open_loop``).
    - ``open_loop``: fire on schedule without semaphore backpressure.
    """

    def __init__(
        self,
        client,
        requests: Optional[list[dict[str, Any]]],
        parallel: int,
        *,
        number: Optional[int] = None,
        max_tokens: int = 128,
        temperature: float = 0.0,
        stream: bool = True,
        on_result: Optional[OnResultCallback] = None,
        rate: float = -1.0,
        open_loop: bool = False,
    ):
        self.client = client
        self.requests = requests
        self.parallel = parallel
        self.number = number
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.stream = stream
        self.on_result = on_result
        self.rate = float(rate)
        self.open_loop = bool(open_loop)

    @property
    def has_rate_pacing(self) -> bool:
        return self.rate != -1 and self.rate > 0

    @staticmethod
    def _count_user_turns(request: Any) -> int:
        """Count user turns for multi-turn Avg Turns/Request (EvalScope)."""
        if isinstance(request, dict):
            messages = request.get("messages")
            if isinstance(messages, list) and messages:
                return sum(
                    1
                    for m in messages
                    if isinstance(m, dict) and m.get("role") == "user"
                )
            if request.get("prompt"):
                return 1
        elif isinstance(request, str):
            return 1
        return 0

    async def _dispatch(self, request: Any) -> dict[str, Any]:
        prompt: Optional[str] = None
        messages = None
        tools = None

        if isinstance(request, str):
            prompt = request
        elif isinstance(request, dict):
            prompt = request.get("prompt")
            messages = request.get("messages")
            tools = request.get("tools")
        else:
            raise TypeError(f"Unsupported request type: {type(request)}")

        result = await self.client.generate(
            prompt=prompt,
            messages=messages,
            tools=tools,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            stream=self.stream,
        )
        result["num_input_turns"] = self._count_user_turns(request)
        return result

    @staticmethod
    def _to_benchmark_data(result: dict[str, Any], benchmark_data_cls: Any):
        ttft = result.get("ttft")
        tpot = result.get("tpot")
        bd = benchmark_data_cls(
            success=bool(result.get("success")),
            query_latency=float(result.get("latency") or 0.0),
            first_chunk_latency=float(ttft) if ttft is not None else 0.0,
            time_per_output_token=float(tpot) if tpot is not None else 0.0,
            prompt_tokens=int(result.get("input_tokens") or 0),
            completion_tokens=int(result.get("output_tokens") or 0),
            error=result.get("error"),
            status_code=result.get("status_code"),
            input_num_turns=int(result.get("num_input_turns") or 0),
        )
        # Lossless round-trip for MetricsAggregator.
        bd._foretoken_result = result  # type: ignore[attr-defined]
        return bd

    @staticmethod
    def _from_benchmark_data(bd: Any) -> dict[str, Any]:
        cached = getattr(bd, "_foretoken_result", None)
        if isinstance(cached, dict):
            return cached
        ttft = float(bd.first_chunk_latency or 0.0)
        tpot = float(bd.time_per_output_token or 0.0)
        return {
            "success": bool(bd.success),
            "status_code": bd.status_code,
            "latency": float(bd.query_latency or 0.0),
            "ttft": ttft if ttft > 0 else None,
            "tpot": tpot if tpot > 0 else None,
            "input_tokens": int(bd.prompt_tokens or 0),
            "output_tokens": int(bd.completion_tokens or 0),
            "error": bd.error,
            "num_input_turns": int(bd.input_num_turns or 0),
        }

    def _rate_label(self) -> str:
        mode = "open-loop" if self.open_loop else "closed-loop"
        if not self.has_rate_pacing:
            return f"INF ({mode}, no pacing)"
        return f"{self.rate:g} req/s ({mode}, Poisson pacing)"

    def _build_evalscope_args(self, Arguments: Any, num_prompts: int):
        # EvalScope: rate=-1 means no pacing; open_loop uses parallel=-1
        # (no semaphore backpressure), matching W&B / EvalScope perf.
        rate = self.rate if self.has_rate_pacing else -1
        parallel = -1 if self.open_loop else self.parallel
        es_args = Arguments(
            model=getattr(self.client, "model", "model"),
            url=getattr(self.client, "url", "http://127.0.0.1/v1/chat/completions"),
            parallel=parallel,
            number=num_prompts,
            rate=rate,
            open_loop=self.open_loop,
            duration=None,
            warmup_num=0,
        )
        _scalarize_args(es_args)
        return es_args

    def _prompts(self) -> list[dict[str, Any]]:
        if self.requests is None:
            raise ValueError(
                "No requests loaded. Use --prompt, --dataset / --dataset-path, "
                "or pass requests. --dataset random requires --tokenizer-path."
            )
        if self.number is None:
            return list(self.requests)
        return list(self.requests)[: self.number]

    async def run(self) -> dict[str, Any]:
        from benchmarks.deps.evalscope import require_strategies

        (
            Arguments,
            ClosedLoopStrategy,
            OpenLoopStrategy,
            BenchmarkData,
        ) = require_strategies()

        await self.client.start()
        prompts = self._prompts()

        print("\n================ Benchmark Config ================")
        print(f"Total Requests : {len(prompts)}")
        if self.open_loop:
            print("Parallel       : unlimited (open-loop)")
        else:
            print(f"Parallel       : {self.parallel}")
        print(f"Rate          : {self._rate_label()}")
        print(f"Model         : {self.client.model}")
        print(f"Endpoint      : {self.client.url}")
        print(f"Stream        : {self.stream}")
        print("=" * 50 + "\n")

        es_args = self._build_evalscope_args(Arguments, len(prompts))
        if self.open_loop:
            queue: asyncio.Queue = asyncio.Queue()
        else:
            maxsize = max(
                1,
                int(es_args.parallel) * int(es_args.queue_size_multiplier),
            )
            queue = asyncio.Queue(maxsize=maxsize)

        adapter = _VLLMStrategyClient(self, BenchmarkData)

        async def request_gen():
            for prompt in prompts:
                yield prompt, False

        if self.open_loop:
            strategy = OpenLoopStrategy(
                es_args, None, adapter, queue, request_gen()
            )
        else:
            strategy = ClosedLoopStrategy(
                es_args, None, adapter, queue, request_gen()
            )

        results: list[dict[str, Any]] = []
        start = time.perf_counter()
        pbar = tqdm_asyncio(total=len(prompts), desc="Benchmarking")

        async def _consume() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is _SENTINEL:
                        return
                    if getattr(item, "is_warmup", False):
                        continue
                    results.append(self._from_benchmark_data(item))
                    self._emit_partial(results, start)
                    pbar.update(1)
                finally:
                    queue.task_done()

        consumer = asyncio.create_task(_consume())
        try:
            await strategy.run()
            await queue.put(_SENTINEL)
            await consumer
        finally:
            pbar.close()
            if not consumer.done():
                consumer.cancel()
                try:
                    await consumer
                except asyncio.CancelledError:
                    pass

        end = time.perf_counter()
        await self.client.close()
        print("\nBenchmark finished!")

        return {
            "results": results,
            "total_time": end - start,
        }

    def _emit_partial(self, results: list[dict[str, Any]], start: float) -> None:
        if self.on_result is None:
            return
        self.on_result(
            {
                "results": list(results),
                "total_time": time.perf_counter() - start,
            }
        )

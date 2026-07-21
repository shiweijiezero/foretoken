from __future__ import annotations

import csv
import io
import re
import time
from typing import Any, Optional

from urllib.parse import urlparse, urlunparse

import aiohttp
import numpy as np


def derive_metrics_url(chat_url: str) -> str:
    """Derive `/metrics` from a chat completions URL."""
    parsed = urlparse(chat_url)
    return urlunparse((parsed.scheme, parsed.netloc, "/metrics", "", "", ""))


# Gauges sampled into timeseries.
GAUGE_ALIASES = {
    "num_requests_running": [
        "vllm:num_requests_running",
        "vllm_num_requests_running",
    ],
    "num_requests_waiting": [
        "vllm:num_requests_waiting",
        "vllm_num_requests_waiting",
    ],
    "kv_cache_usage_perc": [
        "vllm:kv_cache_usage_perc",
        "vllm_kv_cache_usage_perc",
        "vllm:gpu_cache_usage_perc",
        "vllm_gpu_cache_usage_perc",
    ],
}

# Counters: report delta over the collection window.
COUNTER_ALIASES = {
    "prefix_cache_queries": [
        "vllm:prefix_cache_queries_total",
        "vllm_prefix_cache_queries_total",
    ],
    "prefix_cache_hits": [
        "vllm:prefix_cache_hits_total",
        "vllm_prefix_cache_hits_total",
    ],
    "prompt_tokens": [
        "vllm:prompt_tokens_total",
        "vllm_prompt_tokens_total",
    ],
    "generation_tokens": [
        "vllm:generation_tokens_total",
        "vllm_generation_tokens_total",
    ],
    "num_preemptions": [
        "vllm:num_preemptions_total",
        "vllm_num_preemptions_total",
    ],
}

_SAMPLE_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?P<labels>\{[^}]*\})?\s+"
    r"(?P<value>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
    r"(?:\s+(?P<ts>\d+(?:\.\d+)?))?\s*$"
)


def parse_prometheus_text(text: str) -> dict[str, float]:
    """Parse Prometheus exposition text into aggregated name → value.

    Multiple labelsets for the same metric name are summed (typical for
    multi-engine deployments). Histogram/summary suffixes are kept as-is;
    callers pick the specific series they need via aliases.
    """
    totals: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_LINE.match(line)
        if not match:
            continue
        name = match.group("name")
        value = float(match.group("value"))
        totals[name] = totals.get(name, 0.0) + value
    return totals


def _pick(raw: dict[str, float], aliases: list[str]) -> Optional[float]:
    for name in aliases:
        if name in raw:
            return float(raw[name])
    return None


def _extract_gauges(raw: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, aliases in GAUGE_ALIASES.items():
        value = _pick(raw, aliases)
        if value is not None:
            out[key] = value
    return out


def _extract_counters(raw: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, aliases in COUNTER_ALIASES.items():
        value = _pick(raw, aliases)
        if value is not None:
            out[key] = value
    return out


class VLLMPrometheusSource:
    """Scrape vLLM Prometheus `/metrics` endpoint."""

    def __init__(
        self,
        metrics_url: str,
        api_key: str = "",
        timeout: float = 10.0,
    ):
        self.metrics_url = metrics_url
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers=headers,
            )
        return self._session

    async def scrape(self) -> dict[str, float]:
        session = await self._ensure_session()
        async with session.get(self.metrics_url) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"Engine metrics HTTP {response.status}: {text[:200]}"
                )
        return parse_prometheus_text(text)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


class EngineMetricsCollector:
    """Background sampler over a metrics source (``scrape`` / ``close``)."""

    def __init__(
        self,
        source: VLLMPrometheusSource,
        interval: float = 1.0,
    ):
        self.source = source
        self.interval = max(float(interval), 0.1)
        self._task: Optional[Any] = None
        self._stop = False
        self._started_at: Optional[float] = None
        self._samples: list[dict[str, Any]] = []
        self._first_counters: Optional[dict[str, float]] = None
        self._last_counters: Optional[dict[str, float]] = None
        self._errors: list[str] = []

    async def start(self) -> None:
        import asyncio

        self._stop = False
        self._started_at = time.perf_counter()
        self._samples = []
        self._first_counters = None
        self._last_counters = None
        self._errors = []
        await self._sample_once()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> dict[str, Any]:
        import asyncio

        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        await self._sample_once()
        await self.source.close()
        return self.result()

    async def _loop(self) -> None:
        import asyncio

        while not self._stop:
            await asyncio.sleep(self.interval)
            if self._stop:
                break
            await self._sample_once()

    async def _sample_once(self) -> None:
        assert self._started_at is not None
        try:
            raw = await self.source.scrape()
        except Exception as e:
            self._errors.append(str(e))
            return

        gauges = _extract_gauges(raw)
        counters = _extract_counters(raw)
        if self._first_counters is None:
            self._first_counters = dict(counters)
        self._last_counters = dict(counters)

        elapsed = time.perf_counter() - self._started_at
        sample = {
            "elapsed_s": elapsed,
            "wall_time": time.time(),
            **gauges,
        }
        self._samples.append(sample)

    def result(self) -> dict[str, Any]:
        summary = self._summarize()
        return {
            "timeseries": self._samples,
            "summary": summary,
            "errors": list(self._errors),
            "ok": len(self._samples) > 0,
        }

    def _summarize(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "sample_count": len(self._samples),
        }
        if not self._samples:
            summary["error"] = (
                self._errors[-1] if self._errors else "no samples"
            )
            return summary

        for key in GAUGE_ALIASES:
            values = [
                float(s[key]) for s in self._samples if key in s
            ]
            if not values:
                continue
            arr = np.asarray(values, dtype=float)
            summary[key] = {
                "mean": float(np.mean(arr)),
                "max": float(np.max(arr)),
                "min": float(np.min(arr)),
                "last": float(values[-1]),
            }

        first = self._first_counters or {}
        last = self._last_counters or {}
        deltas: dict[str, float] = {}
        for key in COUNTER_ALIASES:
            if key in first and key in last:
                deltas[key] = max(float(last[key]) - float(first[key]), 0.0)
        if deltas:
            summary["counter_delta"] = deltas
            queries = deltas.get("prefix_cache_queries", 0.0)
            hits = deltas.get("prefix_cache_hits", 0.0)
            if queries > 0:
                summary["prefix_cache_hit_rate"] = hits / queries
            else:
                summary["prefix_cache_hit_rate"] = None

        if self._errors:
            summary["scrape_errors"] = len(self._errors)
        return summary


def build_engine_collector(
    chat_url: str,
    *,
    metrics_url: str = "",
    api_key: str = "",
    interval: float = 1.0,
) -> EngineMetricsCollector:
    url = metrics_url or derive_metrics_url(chat_url)
    source = VLLMPrometheusSource(url, api_key=api_key)
    return EngineMetricsCollector(source, interval=interval)


def engine_timeseries_csv(timeseries: list[dict[str, Any]]) -> str:
    if not timeseries:
        return ""
    fieldnames = ["elapsed_s", "wall_time"] + list(GAUGE_ALIASES.keys())
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in timeseries:
        writer.writerow(row)
    return buffer.getvalue()

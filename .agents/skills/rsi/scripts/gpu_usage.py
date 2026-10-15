# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Continuously collect host GPU usage and summarize retained time windows."""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path


def timestamp(value: str) -> float:
    """Parse an offset-aware ISO timestamp used for task deadlines and windows."""
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise argparse.ArgumentTypeError("Include a UTC offset in the timestamp")
    return date.timestamp()


def positive_seconds(value: str) -> float:
    """Reject nonpositive or nonfinite collection intervals at the CLI boundary."""
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive number of seconds")
    return number


def utilization(value: str | float) -> float | None:
    """Normalize a device reading, retaining unavailable counters as missing."""
    if str(value).strip() in {"N/A", "[N/A]", "[Not Supported]"}:
        return None
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 100:
        raise ValueError(f"Invalid GPU utilization: {value!r}")
    return number


def query_gpus(backend: str, indices: list[int] | None, timeout: float) -> list[dict]:
    """Read host device counters through the selected vendor's read-only CLI."""
    if backend == "nvidia":
        command = [
            "nvidia-smi",
            "--query-gpu=index,uuid,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
    else:
        command = ["mx-smi", "--show-usage", "--json"]
    if indices is not None:
        command.extend(["-i", ",".join(map(str, indices))])
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"{command[0]} exited {result.returncode}"
        )
    readings = []
    if backend == "nvidia":
        for row in csv.reader(result.stdout.splitlines()):
            index, identity, usage = row
            readings.append(
                {
                    "index": int(index),
                    "id": identity.strip(),
                    "utilization_percent": utilization(usage),
                }
            )
    else:
        text = result.stdout.strip()
        # mx-smi prints a version banner before its JSON on supported hosts.
        if text.startswith("mx-smi  version:"):
            text = text.split("\n", 1)[1]
        devices = json.loads(text)
        for identity, device in devices.items():
            if isinstance(device, dict) and "device_id" in device:
                readings.append(
                    {
                        "index": int(device["device_id"]),
                        "id": identity,
                        "utilization_percent": utilization(device["utilization"]),
                    }
                )
    if not readings:
        raise ValueError("Device query returned no GPUs")
    if indices is not None and set(indices) != {row["index"] for row in readings}:
        raise ValueError("Device query did not return the requested GPU indices")
    return readings


def collect(args: argparse.Namespace) -> None:
    """Own one foreground collector; persist samples until deadline, signal, or stop file."""
    backend = args.backend
    if backend == "auto":
        if shutil.which("nvidia-smi"):
            backend = "nvidia"
        elif shutil.which("mx-smi"):
            backend = "metax"
        else:
            raise ValueError("Neither nvidia-smi nor mx-smi is available")
    if args.deadline is not None and args.deadline <= time.time():
        raise ValueError("Task deadline has already passed")
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    if (output / "stop").exists():
        raise ValueError("Use a new output directory when resuming collection")
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stopped.set())

    # Exclusive creation prevents duplicate collectors and preserves earlier runs.
    with (output / "samples.jsonl").open("x", buffering=1) as stream:

        def emit(record: dict) -> None:
            stream.write(json.dumps(record, allow_nan=False) + "\n")

        emit(
            {
                "event": "start",
                "timestamp": time.time(),
                "node": socket.gethostname(),
                "pid": os.getpid(),
                "backend": backend,
                "interval_seconds": args.interval,
                "gpu_indices": args.gpus,
                "deadline": args.deadline,
            }
        )
        print(
            json.dumps({"pid": os.getpid(), "output": str(output.resolve())}),
            flush=True,
        )
        reason = "failed"
        try:
            while not stopped.is_set() and not (output / "stop").exists():
                now = time.time()
                remaining = (
                    args.deadline - now if args.deadline is not None else math.inf
                )
                if remaining <= 0:
                    reason = "deadline"
                    break
                tick = time.monotonic()
                record = {"event": "sample", "timestamp": now, "gpus": []}
                try:
                    record["gpus"] = query_gpus(
                        backend, args.gpus, min(10.0, remaining)
                    )
                except (
                    OSError,
                    subprocess.TimeoutExpired,
                    RuntimeError,
                    ValueError,
                ) as exc:
                    record["error"] = str(exc)
                emit(record)
                next_tick = tick + args.interval
                while not stopped.is_set() and not (output / "stop").exists():
                    delay = next_tick - time.monotonic()
                    if args.deadline is not None:
                        delay = min(delay, args.deadline - time.time())
                    if delay <= 0:
                        break
                    stopped.wait(min(delay, 1.0))
            if reason == "failed":
                reason = "stopped"
        finally:
            emit({"event": "end", "timestamp": time.time(), "reason": reason})


def read_collector(directory: Path, since: float, until: float) -> tuple[dict, list[dict]]:
    """Read complete records and retain window diagnostics and clock ambiguity."""
    records = []
    end = None
    last_sample = last_success = None
    errors = []
    clock_windows = []
    with (directory / "samples.jsonl").open() as stream:
        first = stream.readline()
        if not first.endswith("\n"):
            raise ValueError(f"Collector metadata is not yet complete: {directory}")
        metadata = json.loads(first)
        if metadata["event"] != "start":
            raise ValueError(f"Missing collector start record: {directory}")
        high_water = metadata["timestamp"]
        for line in stream:
            # A live writer may have only appended part of its last record.
            if not line.endswith("\n"):
                break
            record = json.loads(line)
            records.append(record)
            stamp = record["timestamp"]
            # A rollback makes repeated wall-clock windows ambiguous in this collector.
            if stamp < high_water:
                lower = stamp
                while clock_windows and clock_windows[-1]["until"] >= lower:
                    lower = min(lower, clock_windows.pop()["since"])
                clock_windows.append({"since": lower, "until": high_water})
            high_water = max(high_water, stamp)
            if record["event"] == "end":
                end = record if stamp <= until else None
                break
            # Later file records can return to the window after a clock rollback.
            if stamp > until:
                continue
            last_sample = stamp
            if any(gpu["utilization_percent"] is not None for gpu in record["gpus"]):
                last_success = stamp
            if "error" in record and stamp >= since:
                errors.append({"timestamp": stamp, "message": record["error"]})
    return {
        "node": metadata["node"],
        "pid": metadata["pid"],
        "backend": metadata["backend"],
        "output": str(directory),
        "started_at": metadata["timestamp"],
        "interval_seconds": metadata["interval_seconds"],
        "requested_gpu_indices": metadata["gpu_indices"],
        "end": end,
        "last_sample_at": last_sample,
        "last_success_at": last_success,
        "errors": errors,
        "unknown_clock_windows": clock_windows,
    }, records


def summarize_gpu(observations: list[tuple], since: float, until: float) -> dict:
    """Integrate disjoint GPU-seconds using the newest still-valid numeric reading."""
    events: dict[float, list[tuple]] = {}
    for stamp, stop, usage, started_at, output, sequence in observations:
        start = max(since, stamp)
        stop = min(until, stop)
        if stop <= start:
            continue
        # Ties prefer the later collector start, lexical first path, then later row.
        events.setdefault(start, []).append(
            (-stamp, -started_at, output, -sequence, stop, usage)
        )
        events.setdefault(stop, [])

    # Unavailable readings end only their own collector's previous interval;
    # another collector's numeric reading remains usable until its own expiry.
    active: list[tuple] = []
    covered = idle = weighted = 0.0
    boundaries = sorted(events)
    for start, stop in zip(boundaries, boundaries[1:]):
        for observation in events[start]:
            heapq.heappush(active, observation)
        while active and active[0][4] <= start:
            heapq.heappop(active)
        if not active:
            continue
        usage = active[0][5]
        duration = stop - start
        covered += duration
        weighted += usage * duration
        if usage == 0:
            idle += duration
    return {
        "covered_seconds": covered,
        "idle_seconds": idle,
        "mean_utilization_percent": weighted / covered if covered else None,
        "coverage_percent": 100 * covered / (until - since),
    }


def aggregate_usage(items: list[dict], expected_gpu_seconds: float | None) -> dict:
    """Combine GPU summaries by covered time for their node's full-window scope."""
    covered = sum(item["covered_seconds"] for item in items)
    weighted = sum(
        item["mean_utilization_percent"] * item["covered_seconds"]
        for item in items
        if item["mean_utilization_percent"] is not None
    )
    return {
        "covered_seconds": covered,
        "idle_seconds": sum(item["idle_seconds"] for item in items),
        "expected_gpu_seconds": expected_gpu_seconds if items else None,
        "mean_utilization_percent": weighted / covered if covered else None,
        "coverage_percent": 100 * covered / expected_gpu_seconds
        if expected_gpu_seconds
        else None,
    }


def summarize(directories: list[Path], since: float, until: float) -> dict:
    """Merge recorded node/device labels into full-window node summaries."""
    nodes: dict[str, dict] = {}
    for directory in sorted({path.resolve() for path in directories}):
        collector, records = read_collector(directory, since, until)
        node = nodes.setdefault(
            collector.pop("node"),
            {
                "collectors": [],
                "devices": {},
                "requested_indices": set(),
                "scope_known": True,
            },
        )
        node["collectors"].append(collector)
        backend = collector["backend"]
        node["requested_indices"].update(
            (backend, index) for index in collector["requested_gpu_indices"] or []
        )
        has_inventory = False
        for sequence, record in enumerate(records):
            stamp = record["timestamp"]
            if record["event"] == "end":
                break
            if stamp > until:
                continue
            has_inventory = has_inventory or bool(record["gpus"])
            stop = min(until, stamp + collector["interval_seconds"])
            if sequence + 1 < len(records):
                stop = min(stop, records[sequence + 1]["timestamp"])
            # Keep reliable time before a rollback, but not readings originating
            # within the repeated clock window or their extrapolated tails.
            for clock_window in collector["unknown_clock_windows"]:
                if clock_window["since"] <= stamp < clock_window["until"]:
                    stop = stamp
                    break
                if stamp < clock_window["since"] < stop:
                    stop = clock_window["since"]
                    break
            for gpu in record["gpus"]:
                device = node["devices"].setdefault(
                    (backend, gpu["id"]), {"indices": set(), "observations": []}
                )
                device["indices"].add(gpu["index"])
                if stop <= max(since, stamp) or gpu["utilization_percent"] is None:
                    continue
                device["observations"].append(
                    (
                        stamp,
                        stop,
                        gpu["utilization_percent"],
                        collector["started_at"],
                        collector["output"],
                        sequence,
                    )
                )
        if collector["requested_gpu_indices"] is None and not has_inventory:
            node["scope_known"] = False

    summaries = []
    window = until - since
    for name, node in sorted(nodes.items()):
        gpus = []
        observed_indices = set()
        for (backend, identity), device in sorted(node["devices"].items()):
            observed_indices.update((backend, index) for index in device["indices"])
            gpus.append(
                {
                    "backend": backend,
                    "id": identity,
                    "indices": sorted(device["indices"]),
                    **summarize_gpu(device["observations"], since, until),
                }
            )
        # Requested devices with no identity still contribute unknown GPU-seconds.
        for backend, index in sorted(node["requested_indices"] - observed_indices):
            gpus.append(
                {
                    "backend": backend,
                    "id": None,
                    "indices": [index],
                    **summarize_gpu([], since, until),
                }
            )
        summaries.append(
            {
                "node": name,
                **aggregate_usage(
                    gpus, window * len(gpus) if node["scope_known"] else None
                ),
                "gpus": gpus,
                "collectors": node["collectors"],
            }
        )
    return {
        "since": since,
        "until": until,
        "nodes": summaries,
    }


def main() -> None:
    """Expose collection, non-mutating window summaries, and cooperative stop requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser(
        "collect", help="Continuously collect this host's GPUs"
    )
    collect_parser.add_argument(
        "--output", type=Path, required=True, help="New run directory"
    )
    collect_parser.add_argument(
        "--backend", choices=("auto", "nvidia", "metax"), default="auto"
    )
    collect_parser.add_argument(
        "--gpus",
        type=lambda value: list(dict.fromkeys(int(item) for item in value.split(","))),
        help="Comma-separated host GPU indices; default: all visible GPUs",
    )
    collect_parser.add_argument(
        "--interval",
        type=positive_seconds,
        default=10.0,
        help="Sampling interval in seconds (default: 10)",
    )
    collect_parser.add_argument(
        "--deadline", type=timestamp, help="Absolute task deadline with UTC offset"
    )
    summary_parser = commands.add_parser(
        "summarize", help="Summarize retained samples without interrupting collection"
    )
    summary_parser.add_argument("directories", type=Path, nargs="+")
    summary_parser.add_argument("--since", type=timestamp, required=True)
    summary_parser.add_argument(
        "--until", type=timestamp, help="Window end; defaults to now"
    )
    stop_parser = commands.add_parser(
        "stop", help="Request a collector to stop; samples are preserved"
    )
    stop_parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "collect":
            collect(args)
        elif args.command == "stop":
            if not (args.directory / "samples.jsonl").is_file():
                raise ValueError(
                    "Collector samples.jsonl does not exist in this directory"
                )
            (args.directory / "stop").touch()
        else:
            until = args.until if args.until is not None else time.time()
            if until <= args.since or until > time.time():
                raise ValueError("Summary window must have since < until <= now")
            print(
                json.dumps(
                    summarize(args.directories, args.since, until),
                    indent=2,
                    allow_nan=False,
                )
            )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

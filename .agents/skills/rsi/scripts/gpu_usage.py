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
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
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
    """Read UUID-identified counters through the vendor CLI within one query budget."""
    deadline = time.monotonic() + timeout

    def invoke(command: list[str]) -> str:
        """Run a vendor read using the remaining query budget."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("GPU query deadline exceeded")
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=remaining, check=False
        )
        if result.returncode:
            raise RuntimeError(
                result.stderr.strip()
                or result.stdout.strip()
                or f"{command[0]} exited {result.returncode}"
            )
        return result.stdout

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
    text = invoke(command).strip()
    readings = []
    if backend == "nvidia":
        for row in csv.reader(text.splitlines()):
            index, identity, usage = row
            readings.append(
                {
                    "index": int(index),
                    "id": identity.strip(),
                    "utilization_percent": utilization(usage),
                }
            )
    else:
        # Usage is keyed by PCI location; the device list supplies physical UUIDs.
        if text.startswith("mx-smi  version:"):
            text = text.split("\n", 1)[1]
        devices = {
            address: value
            for address, value in json.loads(text).items()
            if isinstance(value, dict) and "device_id" in value
        }
        identities = {}
        for line in invoke(["mx-smi", "-L"]).splitlines():
            match = re.search(r"\(UUID:\s*([^\s)]+)\)", line)
            if match:
                identities.update(
                    {
                        address: match.group(1)
                        for address in devices
                        if address in line.split()
                    }
                )
        for address, device in devices.items():
            if address not in identities:
                raise ValueError(f"Device list has no UUID for PCI address {address}")
            readings.append(
                {
                    "index": int(device["device_id"]),
                    "id": identities[address],
                    "pci_bus_id": address,
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

    try:
        machine = uuid.UUID(Path("/etc/machine-id").read_text().strip())
        host_id = machine.hex if machine.int else None
    except (OSError, ValueError):
        host_id = None

    # Exclusive creation preserves earlier runs; identities survive copied files and renamed hosts.
    with (output / "samples.jsonl").open("x", buffering=1) as stream:
        origin = time.monotonic()
        sequence = 0

        def emit(record: dict) -> None:
            """Append a stable record sequence so copied snapshots can be combined."""
            nonlocal sequence
            stream.write(
                json.dumps({"sequence": sequence, **record}, allow_nan=False) + "\n"
            )
            sequence += 1

        emit(
            {
                "event": "start",
                "timestamp": time.time(),
                "elapsed_seconds": 0.0,
                "collector_id": str(uuid.uuid4()),
                "host_id": host_id,
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
                record = {
                    "event": "sample",
                    "timestamp": now,
                    "elapsed_seconds": tick - origin,
                    "gpus": [],
                }
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
            emit(
                {
                    "event": "end",
                    "timestamp": time.time(),
                    "elapsed_seconds": time.monotonic() - origin,
                    "reason": reason,
                }
            )


def read_collectors(directories: list[Path]) -> list[tuple]:
    """Merge append-only snapshots by collector and sequence, reading file aliases once."""
    collectors: dict[tuple, dict] = {}
    files: dict[tuple[int, int], tuple] = {}
    for directory in sorted({path.resolve() for path in directories}):
        path = directory / "samples.jsonl"
        info = path.stat()
        file_key = (info.st_dev, info.st_ino)
        if file_key in files:
            collectors[files[file_key]]["outputs"].append(str(directory))
            continue
        with path.open() as stream:
            first = stream.readline()
            if not first.endswith("\n"):
                raise ValueError(f"Collector metadata is not yet complete: {directory}")
            metadata = json.loads(first)
            if metadata["event"] != "start":
                raise ValueError(f"Missing collector start record: {directory}")
            identity = (
                ("collector", metadata["collector_id"])
                if metadata.get("collector_id")
                else (
                    "legacy",
                    metadata.get("host_id") or metadata["node"],
                    metadata["backend"],
                    metadata["pid"],
                    metadata["timestamp"],
                )
            )
            collector = collectors.setdefault(
                identity, {"metadata": metadata, "records": {}, "outputs": []}
            )
            if collector["metadata"] != metadata:
                raise ValueError(
                    f"Conflicting metadata for the same collector: {directory}"
                )
            collector["outputs"].append(str(directory))
            files[file_key] = identity
            for position, line in enumerate(stream, 1):
                # A live writer may have only appended part of its last record.
                if not line.endswith("\n"):
                    break
                record = json.loads(line)
                sequence = record.get("sequence", position)
                previous = collector["records"].get(sequence)
                if previous is not None and previous != record:
                    raise ValueError(
                        f"Conflicting collector record {sequence}: {directory}"
                    )
                collector["records"][sequence] = record
                if record["event"] == "end":
                    break
    return [
        (
            identity,
            item["metadata"],
            [record for _, record in sorted(item["records"].items())],
            sorted(item["outputs"]),
        )
        for identity, item in sorted(collectors.items())
    ]


def merge_clock_windows(windows: list[dict]) -> list[dict]:
    """Unify ambiguous UTC intervals recorded by collectors sharing a host clock."""
    merged: list[dict] = []
    for window in sorted(windows, key=lambda item: (item["since"], item["until"])):
        if merged and window["since"] <= merged[-1]["until"]:
            merged[-1]["until"] = max(merged[-1]["until"], window["until"])
        else:
            merged.append(dict(window))
    return merged


def collector_window(
    metadata: dict, records: list[dict], outputs: list[str], since: float, until: float
) -> dict:
    """Describe one merged collector's window, retaining clock ambiguity and query failures."""
    end = None
    last_sample = last_success = None
    errors = []
    clock_windows = []
    high_water = metadata["timestamp"]
    for record in records:
        stamp = record["timestamp"]
        # Repeated wall-clock windows cannot be located uniquely, even with elapsed time.
        if stamp < high_water:
            clock_windows.append({"since": stamp, "until": high_water})
        high_water = max(high_water, stamp)
        if record["event"] == "end":
            end = record
            break
        if stamp > until:
            continue
        last_sample = stamp
        if any(gpu["utilization_percent"] is not None for gpu in record["gpus"]):
            last_success = stamp
        if "error" in record and stamp >= since:
            errors.append({"timestamp": stamp, "message": record["error"]})
    return {
        "node": metadata["node"],
        "host_id": metadata.get("host_id"),
        "collector_id": metadata.get("collector_id"),
        "pid": metadata["pid"],
        "backend": metadata["backend"],
        "outputs": outputs,
        "started_at": metadata["timestamp"],
        "interval_seconds": metadata["interval_seconds"],
        "requested_gpu_indices": metadata["gpu_indices"],
        "end": end,
        "last_sample_at": last_sample,
        "last_success_at": last_success,
        "errors": errors,
        "unknown_clock_windows": merge_clock_windows(clock_windows),
    }


def summarize_gpu(observations: list[tuple], since: float, until: float) -> dict:
    """Integrate disjoint GPU-seconds using the newest still-valid numeric reading."""
    events: dict[float, list[tuple]] = {}
    for stamp, stop, usage, started_at, collector_id, sequence in observations:
        start = max(since, stamp)
        stop = min(until, stop)
        if stop <= start or usage is None:
            continue
        # Stable collector identity, not the location of a copied file, breaks ties.
        events.setdefault(start, []).append(
            (-stamp, -started_at, collector_id, -sequence, stop, usage)
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
    """Normalize collector identities and integrate each host's full-window device observations."""
    inputs = read_collectors(directories)
    host_ids: dict[str, set[str]] = {}
    for _, metadata, _, _ in inputs:
        if metadata.get("host_id"):
            host_ids.setdefault(metadata["node"], set()).add(metadata["host_id"])

    def host_identity(metadata: dict) -> tuple[str, str]:
        """Resolve a captured host ID or an unambiguous recorded-name alias."""
        if metadata.get("host_id"):
            return "machine-id", metadata["host_id"]
        aliases = host_ids.get(metadata["node"], set())
        return (
            ("machine-id", next(iter(aliases)))
            if len(aliases) == 1
            else ("hostname", metadata["node"])
        )

    # Bridge old hostname/PCI records only when the supplied evidence identifies
    # one host and one physical device; retain ambiguity instead of guessing indices.
    nodes: dict[tuple, list[tuple]] = {}
    pci_ids: dict[tuple, set[str]] = {}
    uuid_hosts = set()
    for collector_id, metadata, records, paths in inputs:
        host = host_identity(metadata)
        collector = collector_window(metadata, records, paths, since, until)
        nodes.setdefault(host, []).append((collector_id, collector, records))
        if collector["backend"] == "metax":
            for record in records:
                for gpu in record.get("gpus", []):
                    if gpu.get("pci_bus_id"):
                        pci_ids.setdefault(
                            (host, gpu["pci_bus_id"].lower()), set()
                        ).add(gpu["id"])
                    if gpu.get("pci_bus_id") or gpu["id"].startswith("GPU-"):
                        uuid_hosts.add(host)

    summaries = []
    window = until - since
    for host, segments in sorted(nodes.items()):
        collectors = [collector for _, collector, _ in segments]
        clock_windows = merge_clock_windows(
            [
                interval
                for collector in collectors
                for interval in collector["unknown_clock_windows"]
            ]
        )
        devices: dict[tuple, dict] = {}
        requested_indices = set()
        scope_known = True
        ambiguous = host[0] == "hostname" and len(host_ids.get(host[1], set())) > 1
        for collector_id, collector, records in segments:
            backend = collector["backend"]
            requested_indices.update(
                (backend, index) for index in collector["requested_gpu_indices"] or []
            )
            has_inventory = False
            for sequence, record in enumerate(records):
                stamp = record["timestamp"]
                if record["event"] == "end":
                    break
                has_inventory = has_inventory or bool(record["gpus"])
                stop = min(until, stamp + collector["interval_seconds"])
                if sequence + 1 < len(records):
                    following = records[sequence + 1]
                    stop = min(stop, following["timestamp"])
                    if "elapsed_seconds" in record and "elapsed_seconds" in following:
                        stop = min(
                            stop,
                            stamp
                            + following["elapsed_seconds"]
                            - record["elapsed_seconds"],
                        )
                # A host-clock rollback applies to every collector on that host.
                for interval in clock_windows:
                    if interval["since"] <= stamp < interval["until"]:
                        stop = stamp
                        break
                    if stamp < interval["since"] < stop:
                        stop = interval["since"]
                        break
                for gpu in record["gpus"]:
                    identity = gpu["id"]
                    if (
                        backend == "metax"
                        and not gpu.get("pci_bus_id")
                        and not identity.startswith("GPU-")
                    ):
                        aliases = pci_ids.get((host, identity.lower()), set())
                        if len(aliases) == 1:
                            identity = next(iter(aliases))
                        elif host in uuid_hosts:
                            ambiguous = True
                    device = devices.setdefault(
                        (backend, identity), {"indices": set(), "observations": []}
                    )
                    device["indices"].add(gpu["index"])
                    device["observations"].append(
                        (
                            stamp,
                            stop,
                            gpu["utilization_percent"],
                            collector["started_at"],
                            collector_id,
                            record.get("sequence", sequence + 1),
                        )
                    )
            if collector["requested_gpu_indices"] is None and not has_inventory:
                scope_known = False

        gpus = []
        observed_indices = set()
        for (backend, identity), device in sorted(devices.items()):
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
        for backend, index in sorted(requested_indices - observed_indices):
            gpus.append(
                {
                    "backend": backend,
                    "id": None,
                    "indices": [index],
                    **summarize_gpu([], since, until),
                }
            )
        aggregate = aggregate_usage(gpus, window * len(gpus) if scope_known else None)
        if ambiguous:
            # Individual readings remain visible, but node totals could double-count a device.
            aggregate = {key: None for key in aggregate}
        summaries.append(
            {
                "node": min(item["node"] for item in collectors),
                "host_id": host[1] if host[0] == "machine-id" else None,
                "identity_source": host[0],
                "identity_ambiguous": ambiguous,
                **aggregate,
                "gpus": gpus,
                "collectors": collectors,
                "unknown_clock_windows": clock_windows,
            }
        )
    return {"since": since, "until": until, "nodes": summaries}


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

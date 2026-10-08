# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Calibrate MetaX network ports before replacing the launcher with the engine."""

import importlib.util
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path


def _visible_ports() -> list[str]:
    """Find active, allocated ports after the MetaX adapter excludes unusable RoCE GIDs."""
    from vllm_metax.utils.mccl import configure_mccl_visible_hcas

    configure_mccl_visible_hcas()
    excluded = set(os.environ.get("MCCL_IB_HCA", "").removeprefix("^=").split(","))
    # Resolve each verbs device directly, without assuming a shared parent layout.
    devices = {
        (verbs / "ibdev").read_text().strip()
        for verbs in Path("/sys/class/infiniband_verbs").glob("uverbs*")
        if Path("/dev/infiniband", verbs.name).exists()
    }
    ports = []
    for device_name in sorted(devices):
        device = Path("/sys/class/infiniband", device_name)
        for port in sorted((device / "ports").glob("*")):
            name = f"{device.name}:{port.name}"
            if (
                name not in excluded
                and (port / "state").read_text().split(":", 1)[0].strip() == "4"
            ):
                ports.append(name)
    return ports


def _measure_worker(local_rank: int, options: dict, results) -> None:
    """Measure the same collective on every allocated GPU and release its communicator."""
    # Native libraries also write to fd 1; keep the probe's JSON result separate.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    import torch
    import torch.distributed as dist
    from vllm.plugins import load_general_plugins

    load_general_plugins()
    from vllm.distributed.device_communicators.pynccl import PyNcclCommunicator

    rank = options["rank_offset"] + local_rank
    torch.cuda.set_device(local_rank)
    timeout = timedelta(seconds=options["timeout"])
    store = dist.TCPStore(
        options["leader"],
        options["port"] + 1,
        options["world_size"],
        rank == 0,
        timeout=timeout,
    )
    dist.init_process_group(
        "gloo", store=store, rank=rank, world_size=options["world_size"]
    )
    communicator = None
    try:
        communicator = PyNcclCommunicator(dist.group.WORLD, device=local_rank)
        if communicator.disabled:
            raise RuntimeError(
                "MCCL communicator is unavailable for network calibration"
            )
        rows = []
        # Small payloads expose latency; large payloads exercise concurrent NIC bandwidth.
        for size in (4096, 65536, 1048576, 16777216):
            source = torch.ones(size // 2, dtype=torch.bfloat16, device="cuda")
            output = torch.empty_like(source)
            for _ in range(5):
                communicator.all_reduce(source, output)
            torch.cuda.synchronize()
            if not torch.all(output == options["world_size"]).item():
                raise RuntimeError(
                    "MCCL calibration all-reduce returned an incorrect result"
                )
            samples = []
            for _ in range(5):
                dist.barrier()
                start = time.perf_counter()
                for _ in range(20):
                    communicator.all_reduce(source, output)
                torch.cuda.synchronize()
                duration = torch.tensor(
                    [(time.perf_counter() - start) / 20], dtype=torch.float64
                )
                dist.all_reduce(duration, op=dist.ReduceOp.MAX)
                samples.append(duration.item())
            rows.append({"bytes": size, "seconds": statistics.median(samples)})
        if local_rank == 0:
            results.put(rows)
    finally:
        if communicator is not None:
            communicator.destroy()
        dist.destroy_process_group()


def _probe(options: dict) -> None:
    """Own one candidate's GPU workers until all complete or their startup share expires."""
    import torch.multiprocessing as mp

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    deadline = time.monotonic() + options["timeout"]
    results = mp.get_context("spawn").SimpleQueue()
    workers = mp.spawn(
        _measure_worker,
        args=(options, results),
        nprocs=options["gpu_count"],
        join=False,
    )
    try:
        while not workers.join(timeout=max(0, deadline - time.monotonic())):
            if time.monotonic() >= deadline:
                raise TimeoutError("MCCL network candidate exceeded its startup share")
        print(json.dumps(results.get()), flush=True)
    finally:
        for worker in workers.processes:
            if worker.is_alive():
                worker.kill()
        for worker in workers.processes:
            worker.join()
        results.close()


class McclNetworkCalibration:
    """Coordinate member-local probes and publish one port selection before engine startup."""

    def __init__(self, options: dict):
        import torch
        import torch.distributed as dist

        self.index = options["member_index"]
        self.count = options["member_count"]
        self.leader = options["leader"]
        self.port = options["port"]
        self.deadline = time.monotonic() + options["startup_seconds"]
        explicit = "MCCL_IB_HCA" in os.environ
        member = {
            "explicit": explicit,
            "ports": [] if explicit else _visible_ports(),
            "gpus": torch.cuda.device_count(),
        }
        self.store = dist.TCPStore(
            self.leader,
            self.port,
            self.count,
            self.index == 0,
            timeout=timedelta(seconds=options["startup_seconds"]),
        )
        self.store.set(f"member/{self.index}", json.dumps(member))
        self.members = [
            json.loads(self.store.get(f"member/{index}")) for index in range(self.count)
        ]
        self.trials = 0

    def _measure(self, selection: list[list[str]], timeout: float) -> list[dict] | None:
        """Run one member's workers; the leader uses results only after every member succeeds."""
        options = {
            "leader": self.leader,
            "port": self.port,
            "rank_offset": sum(member["gpus"] for member in self.members[: self.index]),
            "world_size": sum(member["gpus"] for member in self.members),
            "gpu_count": self.members[self.index]["gpus"],
            "timeout": timeout,
        }
        environment = dict(
            os.environ, MCCL_IB_HCA="=" + ",".join(selection[self.index])
        )
        result = subprocess.run(
            [sys.executable, __file__, "probe", json.dumps(options)],
            env=environment,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            sys.stderr.buffer.write(result.stdout + result.stderr)
            sys.stderr.flush()
        rows = json.loads(result.stdout) if result.returncode == 0 else None
        key = f"trial/{self.trials}"
        self.store.set(
            f"{key}/{self.index}", json.dumps({"ok": result.returncode == 0})
        )
        statuses = [
            json.loads(self.store.get(f"{key}/{index}")) for index in range(self.count)
        ]
        self.trials += 1
        if not all(status["ok"] for status in statuses):
            return None
        return rows

    def _select(self) -> list[list[str]]:
        """Greedily remove slower ports, measuring whole-group parallel traffic for each choice."""
        selection = [member["ports"] for member in self.members]
        seen = set()
        best_score = math.inf
        winner = None
        candidates = [selection]
        while candidates:
            improved = False
            for offset, candidate in enumerate(candidates):
                signature = tuple(tuple(ports) for ports in candidate)
                if signature in seen:
                    continue
                seen.add(signature)
                # Divide the remaining startup budget rather than create another timeout setting.
                timeout = (self.deadline - time.monotonic()) / (
                    len(candidates) - offset + 1
                )
                if timeout <= 0:
                    raise TimeoutError(
                        "MCCL calibration exceeded the engine startup deadline"
                    )
                self.store.set(
                    f"command/{self.trials}",
                    json.dumps({"ports": candidate, "timeout": timeout}),
                )
                rows = self._measure(candidate, timeout)
                print(
                    f"MCCL network calibration: ports={candidate}, measurements={rows}",
                    file=sys.stderr,
                    flush=True,
                )
                if rows is not None:
                    score = statistics.mean(math.log(row["seconds"]) for row in rows)
                    if score < best_score:
                        best_score = score
                        winner = candidate
                        improved = True
            if winner is not None and not improved:
                break
            # An active port can still be unreachable. Shrink failed combinations until
            # one is usable; afterwards only explore removals from the measured winner.
            parents = [winner] if winner is not None else candidates
            next_candidates = {}
            for parent in parents:
                for member_index, ports in enumerate(parent):
                    if len(ports) > 1:
                        for removed in range(len(ports)):
                            candidate = [list(values) for values in parent]
                            candidate[member_index].pop(removed)
                            signature = tuple(tuple(ports) for ports in candidate)
                            if signature not in seen:
                                next_candidates[signature] = candidate
            candidates = list(next_candidates.values())
        if winner is None:
            raise RuntimeError("No MCCL network candidate completed on every member")
        self.store.set(f"command/{self.trials}", json.dumps({"selected": winner}))
        return winner

    def run(self) -> None:
        """Keep explicit settings or install the measured selection, then close the rendezvous."""
        try:
            if any(member["explicit"] for member in self.members):
                return
            if any(
                not member["ports"] or not member["gpus"] for member in self.members
            ):
                return
            if all(len(member["ports"]) == 1 for member in self.members):
                return
            if self.index == 0:
                try:
                    selection = self._select()
                except Exception as error:
                    self.store.set(
                        f"command/{self.trials}", json.dumps({"error": str(error)})
                    )
                    raise
            else:
                while True:
                    command = json.loads(self.store.get(f"command/{self.trials}"))
                    if "error" in command:
                        raise RuntimeError(command["error"])
                    if "selected" in command:
                        selection = command["selected"]
                        break
                    self._measure(command["ports"], command["timeout"])
            os.environ["MCCL_IB_HCA"] = "=" + ",".join(selection[self.index])
            print(
                f"MCCL selected ports for member {self.index}: {os.environ['MCCL_IB_HCA']}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            # Members acknowledge the final result before the leader closes its store server.
            self.store.set(f"finished/{self.index}", b"1")
            if self.index == 0:
                self.store.wait([f"finished/{index}" for index in range(self.count)])
            self.store = None


def main() -> None:
    """Run a calibration probe or exec the original engine in its existing managed process group."""
    mode = sys.argv[1]
    options = json.loads(sys.argv[2])
    if mode == "probe":
        _probe(options)
        return
    if importlib.util.find_spec("vllm_metax") is not None:
        from vllm.platforms import current_platform

        if current_platform.device_name == "maca":
            from vllm_metax.utils import mccl

            if hasattr(mccl, "configure_mccl_visible_hcas"):
                McclNetworkCalibration(options).run()
    os.execv(sys.executable, [sys.executable, *sys.argv[3:]])


if __name__ == "__main__":
    main()

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own member discovery, candidate selection and group-wide calibration results."""

import json
import math
import os
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


class McclNetworkCalibration:
    """Coordinate member-local probes and publish one port selection before engine startup."""

    def __init__(self, options: dict):
        import torch
        import torch.distributed as dist
        from vllm import envs

        self.index = options["member_index"]
        self.count = options["member_count"]
        self.leader = options["leader"]
        self.port = options["port"]
        self.deadline = time.monotonic() + options["startup_seconds"]
        skip = "MCCL_IB_HCA" in os.environ or envs.VLLM_DISABLE_PYNCCL
        member = {
            "skip": skip,
            "ports": [] if skip else _visible_ports(),
            "gpus": 0 if skip else torch.cuda.device_count(),
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
        rows = None
        failure = None
        status = {"ok": False}
        try:
            result = subprocess.run(
                [sys.executable, "-m", "foretoken_mccl", "probe", json.dumps(options)],
                env=environment,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                sys.stderr.buffer.write(result.stdout + result.stderr)
                sys.stderr.flush()
            else:
                rows = json.loads(result.stdout)
                status["ok"] = True
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            # Publish orchestration failures on the trial peers are already waiting for.
            # Unlike a failed network candidate, these errors abort calibration everywhere.
            failure = error
            status["error"] = f"{type(error).__name__}: {error}"
        key = f"trial/{self.trials}"
        self.store.set(f"{key}/{self.index}", json.dumps(status))
        statuses = [
            json.loads(self.store.get(f"{key}/{index}")) for index in range(self.count)
        ]
        self.trials += 1
        for index, status in enumerate(statuses):
            if "error" in status:
                raise RuntimeError(
                    f"MCCL calibration member {index} failed: {status['error']}"
                ) from failure
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
        """Honor member opt-outs or install the measured ports, then close the rendezvous."""
        try:
            if any(member["skip"] for member in self.members):
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

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Measure one MCCL candidate with parallel GPU workers and release them on exit."""

import json
import os
import signal
import statistics
import sys
import time
from datetime import timedelta


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
        # Cover latency-bound messages (4/64 KiB) and bandwidth-bound transfers (1/16 MiB).
        message_sizes_bytes = (4 * 1024, 64 * 1024, 1024**2, 16 * 1024**2)
        warmup_iterations = 5
        measurement_samples = 5
        iterations_per_sample = 20
        tensor_dtype = torch.bfloat16
        element_bytes = torch.finfo(tensor_dtype).bits // 8
        measurements = []
        for message_bytes in message_sizes_bytes:
            source = torch.ones(
                message_bytes // element_bytes, dtype=tensor_dtype, device="cuda"
            )
            output = torch.empty_like(source)
            for _ in range(warmup_iterations):
                communicator.all_reduce(source, output)
            torch.cuda.synchronize()
            if not torch.all(output == options["world_size"]).item():
                raise RuntimeError(
                    "MCCL calibration all-reduce returned an incorrect result"
                )
            sample_seconds = []
            for _ in range(measurement_samples):
                dist.barrier()
                start = time.perf_counter()
                for _ in range(iterations_per_sample):
                    communicator.all_reduce(source, output)
                torch.cuda.synchronize()
                max_rank_seconds = torch.tensor(
                    [(time.perf_counter() - start) / iterations_per_sample],
                    dtype=torch.float64,
                )
                dist.all_reduce(max_rank_seconds, op=dist.ReduceOp.MAX)
                sample_seconds.append(max_rank_seconds.item())
            measurements.append(
                {"bytes": message_bytes, "seconds": statistics.median(sample_seconds)}
            )
        if local_rank == 0:
            results.put(measurements)
    finally:
        if communicator is not None:
            communicator.destroy()
        dist.destroy_process_group()


def run_probe(options: dict) -> None:
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

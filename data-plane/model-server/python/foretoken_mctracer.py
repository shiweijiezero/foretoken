# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Worker-local mcTracer sessions controlled through vLLM collective RPC."""

import ctypes
import errno
import os
from pathlib import Path
import pty
import subprocess
import tempfile

from vllm.v1.worker.gpu_worker import Worker as GPUWorker


class Capture:
    """Own one worker's collector and terminal until its native report is exported."""

    def __init__(self, directory: str) -> None:
        self.root = Path(directory)
        self.worker = str(os.getpid())
        self.work = self.root / self.worker
        self.work.mkdir()
        self.log = (self.work / "mctracer.log").open("wb")
        self.master, slave = pty.openpty()
        try:
            # The collector inherits the engine's process group so managed-engine
            # shutdown also owns it if a native operation fails or times out.
            self.process = subprocess.Popen(
                ["mcTracer", "--mctx", "--attach", self.worker, "--odname", "capture"],
                cwd=self.work,
                stdin=slave,
                stdout=slave,
                stderr=slave,
            )
        finally:
            os.close(slave)

    def start(self) -> None:
        """Wait for the SDK's attach acknowledgement before workload execution resumes."""
        output = b""
        while b"Rpc connection established!" not in output:
            data = self.read()
            if not data:
                raise RuntimeError("mcTracer exited before attach acknowledgement")
            output += data
        self._set_collection_enabled(True)

    def _set_collection_enabled(self, enabled: bool) -> None:
        """Gate this worker's recording without changing mcTracer's activity setup."""
        # Disabling individual activity kinds during Graph construction can omit
        # Graph-node kernels from later captures. Preserve the collector's setup.
        operation = ctypes.CDLL("libmcpti.so").mcptiToggleActivityAndCallback
        operation.argtypes = [ctypes.c_bool]
        operation.restype = ctypes.c_int
        result = operation(enabled)
        if result != 0:
            raise RuntimeError(f"mcptiToggleActivityAndCallback failed: {result}")

    def read(self) -> bytes:
        """Drain native output to the retained log; PTY EIO marks the collector's exit."""
        try:
            data = os.read(self.master, 65536)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            return b""
        self.log.write(data)
        self.log.flush()
        return data

    def stop(self) -> None:
        """Flush this worker's GPU events, stop its collector, and retain the native JSON."""
        import torch

        # The CLI stop alone can export an empty report for short workloads. MCPTI owns
        # these buffers in the application process, so flushing belongs in the worker.
        torch.cuda.synchronize()
        mcpti = ctypes.CDLL("libmcpti.so")
        flush = mcpti.mcptiActivityFlushAll
        flush.argtypes = [ctypes.c_uint32]
        flush.restype = ctypes.c_int
        result = flush(0)
        if result != 0:
            raise RuntimeError(f"mcptiActivityFlushAll failed: {result}")
        os.write(self.master, b"\x14")
        while self.read():
            pass
        result = self.process.wait()
        os.close(self.master)
        self.log.close()
        if result != 0:
            raise RuntimeError(f"mcTracer exited with status {result}")
        # Collector shutdown can change SDK recording state. Establish the idle
        # state after export, before returning control to model execution.
        self._set_collection_enabled(False)
        (self.work / "capture" / f"tracer_out-{self.worker}.json").rename(
            self.root / f"{self.worker}.mctracer.json"
        )


class Worker(GPUWorker):
    """Prepare selected MetaX workers and expose captures to the runtime supervisor."""

    def init_device(self) -> None:
        """Prepare mcTracer before model loading can install Triton's signal handler."""
        super().init_device()
        # The managed-engine startup deadline owns failure teardown. Keep bootstrap
        # artifacts separate from the supervisor's publishable capture directory.
        with tempfile.TemporaryDirectory(
            prefix="mctracer-", dir=os.environ["FORETOKEN_CACHE_MOUNT_PATH"]
        ) as directory:
            capture = Capture(directory)
            capture.start()
            capture.stop()

    def foretoken_mctracer(self, start: bool, directory: str) -> None:
        """Start or export one capture; model-server owns deadlines and failure teardown."""
        if start:
            self._foretoken_mctracer_capture = Capture(directory)
            self._foretoken_mctracer_capture.start()
        else:
            self._foretoken_mctracer_capture.stop()
            del self._foretoken_mctracer_capture

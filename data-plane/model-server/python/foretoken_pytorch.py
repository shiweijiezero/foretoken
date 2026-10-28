# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own stopped PyTorch captures and native exports through vLLM worker utilities."""

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from vllm.profiler.wrapper import TorchProfilerWrapper


class Capture(TorchProfilerWrapper):
    """Keep the native recorder and its one export job alive until status consumes it."""

    def __init__(self, config: Any, worker_name: str, local_rank: int, activities: list[str]) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="profile-export")
        self._export: Future[None] | None = None
        self._completed: Any = None
        self._finished = False
        self._export_error: str | None = None
        self.prefix = worker_name
        self.path = Path(config.torch_profiler_dir) / f"{worker_name}.pt.trace.json"
        super().__init__(config, worker_name, local_rank, activities, self._trace_ready)

    def start(self) -> None:
        """Keep a capture from restarting while its completed profile still owns export work."""
        if self._completed is not None or self._export is not None or self._finished:
            raise RuntimeError("The capture already owns a completed profile")
        super().start()

    def _trace_ready(self, profile: Any) -> None:
        """Retain the result while the native wrapper finishes its stop and optional summaries."""
        if self._completed is not None or self._export is not None or self._finished:
            raise RuntimeError("A capture must produce exactly one completed trace")
        self._completed = profile.profiler.kineto_results

    def stop(self) -> None:
        """Confirm the native stop before exporting its completed result on the owned worker."""
        super().stop()
        self._start_export()

    def _start_export(self) -> None:
        """Transfer the completed result only after the entire native stop has returned."""
        if not self._running and self._completed is not None:
            self._export = self._pool.submit(self._write_trace, self._completed)
            self._completed = None

    def _write_trace(self, result: Any) -> None:
        """Use Kineto's exact writer with the GIL released, retaining its original format."""
        from foretoken_profile_native import save

        self.path.parent.mkdir(parents=True, exist_ok=True)
        save(result, str(self.path))
        # Kineto may log an I/O error without raising. Full trace validation belongs
        # to the runtime's existing publication stage after native export finishes.
        if not self.path.is_file() or self.path.stat().st_size == 0:
            raise RuntimeError(f"Native profiler did not write {self.path}")

    def status(self) -> dict[str, Any]:
        """Inspect and consume a finished export without waiting inside the worker RPC."""
        if self._export is not None and self._export.done():
            try:
                self._export.result()
            except (OSError, RuntimeError) as error:
                self._export_error = str(error)
            self._export = None
            self._finished = True
            self._pool.shutdown(wait=True)
            # The large native activity buffers are no longer needed by inference.
            self.profiler = None
        return {
            "recording_stopped": not self._running and not self._active,
            "exported": self._finished and self._export_error is None,
            "error": self._export_error,
        }

    def shutdown(self) -> None:
        """Stop recording and join the owned exporter before the worker can exit."""
        super().shutdown()
        self._start_export()
        self._pool.shutdown(wait=True)
        self.status()


class WorkerExtension:
    """Add capture utilities to the platform-selected worker without replacing its lifecycle."""

    def foretoken_pytorch(self, action: str, prefix: str) -> dict[str, Any]:
        """Start, confirm native recording stop, or inspect this runtime's retained export."""
        from vllm.distributed.utils import get_worker_rank_suffix

        name = f"{prefix}_{get_worker_rank_suffix(global_rank=self.rank)}"
        capture = self.profiler
        if action == "start":
            if capture is not None:
                if not isinstance(capture, Capture) or not capture.status()["recording_stopped"]:
                    raise RuntimeError("A native profiler is already active")
                if not capture._finished:
                    raise RuntimeError("The previous capture still owns an export")
            config = self.profiler_config
            configured = getattr(config, "torch_profiler_activities", None)
            capture = Capture(config, name, self.local_rank, configured or ["CPU", "CUDA"])
            self.profiler = capture
            capture.start()
        else:
            if not isinstance(capture, Capture) or capture.prefix != name:
                raise RuntimeError("The requested capture does not belong to this worker")
            if action == "stop":
                capture.stop()
            elif action != "status":
                raise ValueError(f"Unknown profiler action: {action}")
        capture._raise_if_failed()
        return capture.status()

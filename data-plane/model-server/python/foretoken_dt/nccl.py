# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Private two-GPU tensor transfers, independent of vLLM's model process groups."""

import logging
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from queue import SimpleQueue

import torch
from vllm.distributed.device_communicators.pynccl_wrapper import (
    NCCLLibrary,
    buffer_type,
    cudaStream_t,
    ncclComm_t,
    ncclDataTypeEnum,
    ncclUniqueId,
)

logger = logging.getLogger(__name__)


@dataclass
class _Transfer:
    """Retain message storage until completion or allocator-protected teardown."""

    done: torch.cuda.Event
    deadline: float
    tensors: list[torch.Tensor]


class NcclChannel:
    """Own one DT communicator and stream; messages own the transferred tensors."""

    def __init__(self, device: torch.device, timeout_ms: int):
        """Load vLLM's NCCL binding without creating a torch distributed group."""
        self.device = torch.device(device)
        self.timeout_seconds = timeout_ms / 1000
        self.library = NCCLLibrary()
        self.comm: ncclComm_t | None = None
        self.stream = torch.cuda.Stream(device=self.device)
        self._condition = threading.Condition()
        self._pending: deque[_Transfer] = deque()
        self._error: BaseException | None = None
        self._stopping = False
        self._watchdog: threading.Thread | None = None
        self._abort_thread: threading.Thread | None = None
        self._submitter: threading.Thread | None = None
        self._submit_queue = SimpleQueue()
        self._submission: Future[torch.cuda.Event] | None = None

    def unique_id(self) -> list[int]:
        """Return the Target-created rendezvous ID for the JSON control channel."""
        return list(self.library.ncclGetUniqueId().internal)

    def connect(self, unique_id: list[int], rank: int) -> None:
        """Join the private pair after ZMQ has acknowledged its rendezvous ID.

        NCCL's blocking communicator initialization has its own bootstrap failure
        behavior; the tensor-completion deadline does not bound this C call.
        """
        if len(unique_id) != 128 or any(
            type(value) is not int or not -128 <= value <= 127 for value in unique_id
        ):
            raise ValueError("Invalid NCCL rendezvous ID")
        identifier = ncclUniqueId()
        identifier.internal[:] = unique_id
        self.peer = 1 - rank
        with torch.cuda.device(self.device):
            self.comm = self.library.ncclCommInitRank(2, identifier, rank)
        self._watchdog = threading.Thread(
            target=self._watch_completions, name="dt-nccl-completion", daemon=True
        )
        self._watchdog.start()

    def send(self, tensors: list[torch.Tensor]) -> None:
        """Enqueue sends and order the producer's future writes after completion."""
        done = self.enqueue_send(tensors)
        if done is not None:
            # Native vLLM also overwrites persistent buffers in place.
            torch.cuda.current_stream(self.device).wait_event(done)

    def enqueue_send(self, tensors: list[torch.Tensor]) -> torch.cuda.Event | None:
        """Send on the communication stream; the caller orders subsequent source writes."""
        self._join_submission()
        self.check_error()
        if not tensors:
            return None
        producer = torch.cuda.current_stream(self.device)
        with torch.cuda.device(self.device), torch.cuda.stream(self.stream):
            self.stream.wait_stream(producer)
            return self._transfer(tensors, sending=True)

    def enqueue_send_async(self, tensors: list[torch.Tensor]) -> None:
        """Post prefill sends in the background after their original producer's event."""
        self._join_submission()
        self.check_error()
        if not tensors:
            return
        ready = torch.cuda.Event()
        ready.record(torch.cuda.current_stream(self.device))
        if self._submitter is None:
            thread = threading.Thread(
                target=self._submit_sends, name="dt-nccl-submit", daemon=True
            )
            thread.start()
            self._submitter = thread
        submission = Future()
        self._submission = submission
        # The queue and worker own both the original tensors and producer event
        # until _transfer takes over their GPU-completion lifetime.
        self._submit_queue.put((submission, list(tensors), ready))

    def _submit_sends(self) -> None:
        """Own ordered CPU submissions while model execution proceeds on Target's thread."""
        while (work := self._submit_queue.get()) is not None:
            submission, tensors, ready = work
            if not submission.set_running_or_notify_cancel():
                continue
            try:
                with (
                    torch.inference_mode(),
                    torch.cuda.device(self.device),
                    torch.cuda.stream(self.stream),
                ):
                    self.stream.wait_event(ready)
                    done = self._transfer(tensors, sending=True)
                submission.set_result(done)
            except BaseException as error:  # noqa: BLE001
                # Every accepted task must resolve its future, including a
                # failure in the background thread's CUDA context or teardown.
                try:
                    self.fail(error)
                finally:
                    submission.set_exception(error)
            finally:
                del work, tensors, ready

    def _join_submission(self) -> None:
        """Keep subsequent stream submissions behind the prefill's CPU enqueue operation."""
        if self._submission is not None:
            submission, self._submission = self._submission, None
            submission.result()

    def receive(self, tensors: list[torch.Tensor]) -> None:
        """Enqueue receives and make them readable on the caller's current stream."""
        self.check_error()
        if tensors:
            self.receive_groups(tensors)

    def receive_groups(
        self,
        first: list[torch.Tensor],
        second: list[torch.Tensor] | None = None,
        *,
        defer_second: bool = False,
    ) -> torch.cuda.Event:
        """Order the consumer after its first usable group and return final completion."""
        self._join_submission()
        consumer = torch.cuda.current_stream(self.device)
        with torch.cuda.device(self.device), torch.cuda.stream(self.stream):
            # torch.empty can reuse storage with earlier work queued on its
            # allocation stream; order the NCCL writes after that work.
            self.stream.wait_stream(consumer)
            done = self._transfer(first, sending=False)
            first_ready = done
            if second is not None:
                done = self._transfer(second, sending=False)
        consumer.wait_event(first_ready if defer_second else done)
        # The caller may drop the message immediately after queuing a copy.
        # Keep allocator reuse ordered after the consumer as well as the receive.
        for tensor in first:
            tensor.record_stream(consumer)
        if second is not None:
            for tensor in second:
                tensor.record_stream(consumer)
        return done

    def receive_prefill(
        self,
        known: list[torch.Tensor],
        hidden: list[torch.Tensor],
        feedback: list[torch.Tensor],
    ) -> tuple[torch.cuda.Event, torch.cuda.Event]:
        """Let Draft prepare known inputs before waiting for features and sampling."""
        self._join_submission()
        self.check_error()
        consumer = torch.cuda.current_stream(self.device)
        with torch.cuda.device(self.device), torch.cuda.stream(self.stream):
            self.stream.wait_stream(consumer)
            known_ready = self._transfer(known, sending=False)
            hidden_ready = self._transfer(hidden, sending=False)
            feedback_ready = self._transfer(feedback, sending=False)
        consumer.wait_event(known_ready)
        for group in (known, hidden, feedback):
            for tensor in group:
                tensor.record_stream(consumer)
        return hidden_ready, feedback_ready

    def _transfer(
        self,
        tensors: list[torch.Tensor],
        *,
        sending: bool,
    ) -> torch.cuda.Event:
        """Own storage before submission and retain it through GPU completion."""
        if any(tensor.device != self.device for tensor in tensors):
            raise ValueError("DT tensors must be on the communicator's CUDA device")
        # The watchdog may abort on another thread. Keep the communicator alive
        # through enqueue and event registration as one ownership transition.
        with self._condition:
            self.check_error()
            comm = self.comm
            if self._stopping or comm is None:
                raise RuntimeError("DT NCCL channel is closed")
            transfer = _Transfer(
                torch.cuda.Event(),
                0.0,
                list(tensors),
            )
            try:
                self._pending.append(transfer)
                buffers = tensors
                if sending:
                    # Originals must outlive asynchronous contiguous copies.
                    # New copies use this stream for both allocation and copying.
                    buffers = [tensor.detach().contiguous() for tensor in tensors]
                    transfer.tensors.extend(buffers)
                operation = self.library.ncclSend if sending else self.library.ncclRecv
                self.library.ncclGroupStart()
                try:
                    for tensor in buffers:
                        # The byte representation handles bool and bfloat16 uniformly.
                        if tensor.numel():
                            operation(
                                buffer_type(tensor.data_ptr()),
                                tensor.numel() * tensor.element_size(),
                                ncclDataTypeEnum.ncclUint8,
                                self.peer,
                                comm,
                                cudaStream_t(self.stream.cuda_stream),
                            )
                finally:
                    self.library.ncclGroupEnd()
                transfer.done.record(self.stream)
                transfer.deadline = time.monotonic() + self.timeout_seconds
            except BaseException as error:
                # Stop the watchdog before it can query an unrecorded event.
                self.fail(error)
                raise
            self._condition.notify()
        return transfer.done

    def check_error(self) -> None:
        """Raise the first asynchronous failure at a transport boundary."""
        with self._condition:
            if self._error is not None:
                raise self._error

    def fail(self, error: BaseException) -> None:
        """Abort pending transfers when the peer or completion watchdog fails."""
        with self._condition:
            if self._stopping:
                return
            self._error = error
            self._stopping = True
            self._condition.notify_all()
        self._start_abort()

    def _watch_completions(self) -> None:
        """Retire completed storage and abort stalled GPU work without blocking callers."""
        try:
            with torch.cuda.device(self.device):
                while True:
                    with self._condition:
                        while not self._pending and not self._stopping:
                            self._condition.wait()
                        if self._stopping:
                            return
                        # Every transfer uses the same stream, so completion is FIFO.
                        while self._pending and self._pending[0].done.query():
                            self._pending.popleft()
                        if not self._pending:
                            continue
                        remaining = self._pending[0].deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                "NCCL tensor transfer timed out; DT session aborted"
                            )
                        self._condition.wait(timeout=min(0.005, remaining))
        except (RuntimeError, TimeoutError) as error:
            logger.error("DT NCCL session failed: %s", error)
            self.fail(error)

    def _start_abort(self) -> threading.Thread | None:
        """Protect unfinished storage with the allocator before starting native abort."""
        with self._condition:
            if self._abort_thread is not None or self.comm is None:
                return self._abort_thread
            # Teardown cannot prove completion, including when native abort fails.
            # Hand unfinished storage back to the allocator before dropping it.
            for transfer in self._pending:
                for tensor in transfer.tensors:
                    tensor.record_stream(self.stream)
            comm, self.comm = self.comm, None

            def abort() -> None:
                try:
                    with torch.cuda.device(self.device):
                        self.library.ncclCommAbort(comm)
                except RuntimeError as error:
                    with self._condition:
                        if self._error is None:
                            self._error = error
                    logger.error("DT NCCL abort failed: %s", error)
                finally:
                    with self._condition:
                        self._pending.clear()

            self._abort_thread = threading.Thread(target=abort, daemon=True)
            self._abort_thread.start()
            return self._abort_thread

    def close(self) -> None:
        """Stop completion monitoring and abort without requiring the peer to close."""
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._submitter is not None:
            if self._submission is not None:
                self._submission.cancel()
            self._submit_queue.put(None)
        abort = self._start_abort()
        # Match vLLM's bounded communicator teardown when CUDA work is pending.
        deadline = time.monotonic() + 5.0
        for thread in (self._watchdog, abort, self._submitter):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))

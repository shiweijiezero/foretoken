# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Keep MRV2's native proposal contract while executing a turn on a Draft peer."""

import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

import numpy as np
import torch
from torch.futures import Future as DeviceFuture
from vllm.utils.platform_utils import is_uva_available
from vllm.v1.utils import record_function_or_nullcontext
from vllm.v1.worker.gpu.async_utils import async_copy_to_np, stream
from vllm.v1.worker.gpu.spec_decode.speculator import BaseSpeculator
from websockets.sync.client import connect

from .exchange import TensorExchange
from .features import FeatureCapture
from .nccl import NcclPeer
from .protocol import (
    PEER_PROTOCOL_VERSION,
    DeviceRequest,
    ExecuteBatch,
    ExecuteResult,
    FinishRequests,
    InitialDeviceRequest,
    OpenPeer,
    PrepareBatch,
    ProposalRequest,
    ProposeBatch,
    Settings,
    TensorRegion,
)


@dataclass
class ProposalSession:
    """Retain cache identity and confirmed intervals acknowledged by the remote Draft."""

    cache_salt: str | None
    token_count: int = 0
    context_count: int = 0
    initial_prompt: tuple[int, ...] | None = None


@dataclass
class StagedProposal:
    """Retain a native batch's input storage until its ordered peer operation completes."""

    request_ids: list[str]
    indices: torch.Tensor
    metadata: np.ndarray | None
    token_ids: list[np.ndarray]
    metadata_device: torch.Tensor | None
    token_sources: list[torch.Tensor]
    token_offsets: list[int]
    feature_rows: int
    features_transferred: Future[None] | None
    prepare_sent: Future[bool] | None
    device_inputs: torch.Tensor | None
    device_inputs_ready: torch.cuda.Event | None
    query_start_loc: list[int]
    temperatures: np.ndarray
    seeds: np.ndarray
    copy_done: torch.cuda.Event
    sources: list[torch.Tensor]


class RemoteProposer(BaseSpeculator):
    """Own one Target worker's remote sessions and return native GPU proposal tensors."""

    supports_mm_inputs = False

    def __init__(self, worker, settings: Settings):
        self.runner = worker.model_runner
        self.config = worker.vllm_config
        self.spec = self.config.speculative_config
        self.settings = settings
        self.device = worker.device
        self.max_reqs = self.config.scheduler_config.max_num_seqs
        self.max_tokens = self.config.scheduler_config.max_num_batched_tokens
        self.k = self.spec.num_speculative_tokens
        self.peer_id = uuid4().hex
        self.sequence = 0
        self.sessions: dict[str, ProposalSession] = {}
        self.token_history_cpu = (
            self.runner.req_states.all_token_ids._uva_buf.np
            if is_uva_available()
            else None
        )
        self.nccl = (
            NcclPeer(NcclPeer.create_unique_ids(), 0, self.device)
            if settings.protocol == "nccl"
            else None
        )
        self.exchange = TensorExchange(settings.protocol) if self.nccl is None else None
        self.feature_capture: FeatureCapture | None = None
        self.prepare_sent: Future[bool] | None = None
        self.device_requests: list[DeviceRequest] = []
        self.control = None
        self.control_scope = None
        self.destination: TensorRegion | None = None
        self.failed = False
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="draft-control"
        )
        # Main-stream events cannot measure a proposal completed by peer IO.
        self.runner.spec_decode_timing_collector = None
        self.result_stream = torch.cuda.Stream(self.device)
        self.draft_tokens = torch.zeros(
            self.max_reqs, self.k, dtype=torch.int64, device=self.device
        )
        self.draft_logits = None
        if self.spec.draft_sample_method == "probabilistic":
            self.draft_logits = torch.zeros(
                self.max_reqs,
                self.k,
                self.config.model_config.get_vocab_size(),
                dtype=torch.float32,
                device=self.device,
            )

    def add_request(self, request):
        """Carry the native cache identity through initial admission and slot rebinding."""
        self.sessions.setdefault(
            request.req_id,
            ProposalSession(
                request.cache_salt,
                initial_prompt=tuple(request.prompt_token_ids)
                if request.prompt_token_ids
                else None,
            ),
        )

    def init_cudagraph_manager(self, mode):
        """Keep Target graph ownership native; the remote Draft captures its own graphs."""

    def capture(self):
        """Remote work is outside the Target's CUDA graph capture."""

    def bind_target_model(self, target_model):
        """Bind stable feature buffers and peer ownership before native graph capture."""
        if not self.spec.uses_draft_model():
            self.feature_capture = FeatureCapture(
                self.config,
                target_model,
                self.exchange,
                self.device,
                self.runner.use_aux_hidden_state_outputs,
            )
        self._open()

    def prepare_target_forward(self, input_batch):
        """Retire feature-buffer reads before the native forward reuses their storage."""
        if self.failed:
            raise RuntimeError("the Draft binding has failed")
        if self.feature_capture is not None:
            self.feature_capture.prepare_forward()
        self.device_requests = []
        if (
            self.nccl is not None
            and self.spec.method in ("eagle", "eagle3")
            and all(
                int(length) + self.k + 1 <= self.config.model_config.max_model_len
                for length in input_batch.seq_lens_cpu_upper_bound[
                    : input_batch.num_reqs
                ]
            )
        ):
            sampling = self.runner.sampler.sampling_states
            for row, request_id in enumerate(input_batch.req_ids):
                session = self.sessions[request_id]
                count = int(input_batch.num_scheduled_tokens[row])
                initial = None
                if session.token_count:
                    if input_batch.is_prefilling_np[row] or count != self.k + 1:
                        self.device_requests.clear()
                        break
                else:
                    prompt = session.initial_prompt
                    if (
                        prompt is None
                        or int(input_batch.num_computed_tokens_np[row]) != 0
                        or int(input_batch.prefill_len_np[row]) != len(prompt)
                        or count != len(prompt)
                    ):
                        self.device_requests.clear()
                        break
                    slot = int(input_batch.idx_mapping_np[row])
                    initial = InitialDeviceRequest(
                        prompt_token_ids=list(prompt),
                        cache_salt=session.cache_salt,
                        temperature=float(sampling.temperature.np[slot])
                        if self.draft_logits is not None
                        else 0.0,
                        seed=int(sampling.seeds.np[slot]),
                    )
                self.device_requests.append(
                    DeviceRequest(
                        request_id=request_id,
                        query_tokens=count,
                        seq_len_upper_bound=int(
                            input_batch.seq_lens_cpu_upper_bound[row]
                        ),
                        initial=initial,
                    )
                )
        if self.nccl is not None:
            self.prepare_sent = self.executor.submit(
                self._prepare_batch,
                input_batch.num_tokens if self.feature_capture is not None else 0,
                input_batch.num_reqs,
                self.device_requests,
            )

    def _prepare_batch(self, rows, num_requests, requests):
        """Reserve native inputs during Target forward, retaining replay when reservation is unavailable."""
        request = PrepareBatch(
            sequence=self.sequence,
            feature_rows=rows,
            num_requests=num_requests,
            requests=requests,
        )
        self.control.send(
            json.dumps(
                {"operation": "prepare", "payload": request.model_dump()},
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        accepted = self._receive_control()["execute"]
        if accepted:
            for request in requests:
                if request.initial is not None:
                    session = self.sessions[request.request_id]
                    session.token_count = len(request.initial.prompt_token_ids)
                    session.initial_prompt = None
        return accepted

    def _open(self) -> None:
        """Retain the control stream that owns every native Draft session for this worker."""
        features = self.feature_capture
        num_features = 0 if features is None else features.destination.shape[0]
        hidden_size = self.config.model_config.get_hidden_size()
        dtype = str(self.config.model_config.dtype).removeprefix("torch.")
        logits = None
        if self.draft_logits is not None and self.exchange is not None:
            logits = self.exchange.allocate(
                "logits",
                [self.max_reqs, self.k, self.config.model_config.get_vocab_size()],
                "float32",
            )
        request = OpenPeer(
            protocol_version=PEER_PROTOCOL_VERSION,
            peer_id=self.peer_id,
            method=self.spec.method,
            vocab_size=self.config.model_config.get_vocab_size(),
            hidden_size=hidden_size,
            num_features=num_features,
            max_tokens=self.max_tokens,
            max_model_len=self.config.model_config.max_model_len,
            max_requests=self.max_reqs,
            num_candidates=self.k,
            feature_dtype=dtype,
            protocol=self.settings.protocol,
            return_probabilities=self.draft_logits is not None,
            logits=logits,
            nccl_ids=self.nccl.unique_ids if self.nccl is not None else None,
        )
        url = urlsplit(self.settings.endpoint.rstrip("/") + "/v1/internal/draft/peers")
        url = url._replace(scheme={"http": "ws", "https": "wss"}[url.scheme])
        scope = connect(
            url.geturl(),
            proxy=None,
            compression=None,
            open_timeout=None,
            close_timeout=0,
            ping_interval=None,
            max_size=None,
        )
        self.control = scope.__enter__()
        try:
            opened = self._exchange_control(request.model_dump())
            if opened["event"] != "opened":
                raise RuntimeError("Draft did not accept the worker binding")
            self.destination = (
                TensorRegion.model_validate(opened["input"])
                if opened["input"] is not None
                else None
            )
            if self.nccl is not None:
                # Both workers enter the native collective after accepting the
                # control binding; neither waits for a connect ACK first.
                self.control.send('{"operation":"connect"}')
                self.nccl.connect()
                self._receive_control()
        except BaseException:
            if self.nccl is not None:
                self.nccl.close(abort=True)
            self.control = None
            scope.__exit__(None, None, None)
            raise
        self.control_scope = scope

    def _exchange_control(self, payload):
        """Send one ordered peer operation and wait for its native completion acknowledgment."""
        self.control.send(json.dumps(payload, separators=(",", ":"), allow_nan=False))
        return self._receive_control()

    def _receive_control(self):
        """Receive one peer acknowledgment or surface its native operation failure."""
        response = json.loads(self.control.recv())
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    def propose(
        self,
        input_batch,
        attn_metadata,
        slot_mappings,
        last_hidden_states,
        aux_hidden_states,
        num_sampled,
        num_rejected,
        last_sampled,
        next_prefill_tokens,
        temperature,
        seeds,
        dp_sync=None,
        dummy_run=False,
        skip_attn_for_dummy_run=False,
        mm_inputs=None,
        is_profile=False,
    ) -> torch.Tensor | DeviceFuture[torch.Tensor]:
        """Stage native inputs and return a future carrying the proposals' device dependency."""
        if dummy_run:
            return self.draft_tokens[: input_batch.num_reqs].zero_()
        if self.failed:
            raise RuntimeError("the Draft binding has failed")
        states = self.runner.req_states
        indices = input_batch.idx_mapping
        metadata = torch.stack(
            (
                num_sampled.to(torch.int64),
                num_rejected.to(torch.int64),
                states.total_len.gpu[indices].to(torch.int64),
                input_batch.positions[
                    input_batch.query_start_loc[: input_batch.num_reqs]
                ].to(torch.int64),
            ),
            dim=1,
        )
        device_inputs = None
        device_inputs_ready = None
        if self.device_requests:
            # The peer uses its own request slots and KV blocks. Only logical
            # token positions and post-verification lengths cross this boundary.
            device_inputs = torch.cat(
                (
                    input_batch.input_ids[: input_batch.num_tokens].to(torch.int64),
                    input_batch.positions[: input_batch.num_tokens],
                    input_batch.seq_lens[: input_batch.num_reqs].to(torch.int64),
                    num_sampled.to(torch.int64),
                    num_rejected.to(torch.int64),
                    last_sampled[indices].reshape(-1).to(torch.int64),
                    states.total_len.gpu[indices].to(torch.int64),
                )
            )
            device_inputs_ready = torch.cuda.Event()
            device_inputs_ready.record(self.runner.main_stream)
        token_sources = []
        token_rows = []
        token_offsets = []
        bonus_tokens = self.runner.model_state.num_new_sampled_tokens_per_step
        for row, request_id in enumerate(input_batch.req_ids):
            end = max(
                int(input_batch.prefill_len_np[row]),
                int(input_batch.seq_lens_cpu_upper_bound[row]) + bonus_tokens,
            )
            slot = int(input_batch.idx_mapping_np[row])
            # Host reconciliation may trail the device result. Absolute offsets
            # let this snapshot include an older confirmed prefix safely.
            start = self.sessions[request_id].token_count
            token_offsets.append(start)
            if self.token_history_cpu is not None:
                # Native token history is pinned host memory accessed through UVA.
                # Read it after copy_done; CUDA's host-to-host copy would block here.
                # Pending proposals retain the request slot until their consumption.
                token_rows.append(self.token_history_cpu[slot, start:end])
            else:
                token_sources.append(states.all_token_ids.gpu[slot, start:end])
        device_turn = bool(self.device_requests)
        copy_stream = self.runner.output_copy_stream
        copy_done = torch.cuda.Event(blocking=True)
        copied_metadata = None
        if device_turn:
            # Execute ACKs carry the Draft's authoritative confirmation. Keep
            # the device snapshot available for the fallback path, but do not
            # start a D2H copy that the fast path will never consume.
            metadata.record_stream(self.runner.main_stream)
            copy_done.record(self.runner.main_stream)
        else:
            try:
                with (
                    record_function_or_nullcontext("DT Target staging"),
                    stream(copy_stream, self.runner.main_stream),
                ):
                    copy_stream.wait_stream(self.runner.main_stream)
                    try:
                        copied_metadata = async_copy_to_np(metadata)
                        for source in token_sources:
                            token_rows.append(async_copy_to_np(source))
                    finally:
                        copy_done.record(copy_stream)
            except BaseException:
                # A partial batch still owns every source and destination until DMA ends.
                self.failed = True
                copy_done.synchronize()
                raise
        sampling = self.runner.sampler.sampling_states
        staged = StagedProposal(
            request_ids=list(input_batch.req_ids),
            indices=indices,
            metadata=copied_metadata,
            token_ids=token_rows,
            metadata_device=metadata if device_turn else None,
            token_sources=token_sources if device_turn else [],
            token_offsets=token_offsets,
            feature_rows=input_batch.num_tokens,
            features_transferred=None,
            prepare_sent=self.prepare_sent,
            device_inputs=device_inputs,
            device_inputs_ready=device_inputs_ready,
            query_start_loc=input_batch.query_start_loc_np.tolist(),
            temperatures=sampling.temperature.np[input_batch.idx_mapping_np],
            seeds=sampling.seeds.np[input_batch.idx_mapping_np],
            copy_done=copy_done,
            sources=[metadata, *token_sources],
        )
        self.prepare_sent = None
        if self.feature_capture is not None:
            staged.features_transferred = self.feature_capture.begin_transfer()
        result = DeviceFuture(devices=[self.device])
        self.executor.submit(self._complete_proposal, staged, result)
        return result

    def _stage_host_inputs(self, staged: StagedProposal) -> None:
        """Materialize host confirmation only when native reservation falls back."""
        if staged.metadata is not None:
            return
        if staged.metadata_device is None:
            raise RuntimeError("device confirmation has no metadata snapshot")
        copy_stream = self.runner.output_copy_stream
        token_rows = list(staged.token_ids)
        try:
            with (
                record_function_or_nullcontext("DT Target staging"),
                stream(copy_stream, self.runner.main_stream),
            ):
                copy_stream.wait_stream(self.runner.main_stream)
                staged.metadata = async_copy_to_np(staged.metadata_device)
                for source in staged.token_sources:
                    token_rows.append(async_copy_to_np(source))
                staged.token_ids = token_rows
                staged.copy_done.record(copy_stream)
        except BaseException:
            self.failed = True
            staged.copy_done.synchronize()
            raise

    def _confirmed_requests(self, staged: StagedProposal) -> list[ProposalRequest]:
        """Read verified inputs after DMA, retaining one source for session reconciliation."""
        self._stage_host_inputs(staged)
        staged.copy_done.synchronize()
        staged.sources.clear()
        requests = []
        for row, request_id in enumerate(staged.request_ids):
            sampled, rejected, total_len, start = staged.metadata[row].tolist()
            session = self.sessions[request_id]
            previous = session.token_count
            if total_len < previous:
                raise RuntimeError(
                    "native request reset without a Draft session release"
                )
            context_start = session.context_count
            feature_offset = 0
            count = 0
            if self.feature_capture is not None:
                left, right = staged.query_start_loc[row : row + 2]
                computed_end = start + right - left - rejected
                if computed_end > context_start:
                    if start > context_start:
                        raise RuntimeError(
                            "Target features skipped an uncommitted interval"
                        )
                    feature_offset = left + context_start - start
                    count = computed_end - context_start
            requests.append(
                ProposalRequest(
                    request_id=request_id,
                    cache_salt=session.cache_salt,
                    generation=total_len,
                    base_length=previous,
                    token_ids=staged.token_ids[row][
                        previous - staged.token_offsets[row] : total_len
                        - staged.token_offsets[row]
                    ].tolist(),
                    context_start=context_start,
                    feature_offset=feature_offset,
                    feature_count=count,
                    temperature=float(staged.temperatures[row])
                    if self.draft_logits is not None
                    else 0.0,
                    seed=int(staged.seeds[row]),
                    generate=sampled > 0,
                )
            )
        return requests

    def _apply_execute_result(
        self, staged: StagedProposal, payload, sequence: int
    ) -> None:
        """Advance Target sessions from Draft-owned confirmation without a host readback."""
        result = ExecuteResult.model_validate(payload)
        if result.sequence != sequence:
            raise RuntimeError("Draft returned a different proposal sequence")
        if len(result.confirmed) != len(staged.request_ids):
            raise RuntimeError("Draft returned incomplete device confirmation state")
        for request_id, confirmed in zip(
            staged.request_ids, result.confirmed, strict=True
        ):
            session = self.sessions[request_id]
            if confirmed.sequence != sequence:
                raise RuntimeError("Draft returned a mismatched device confirmation")
            if confirmed.confirmed_length < session.token_count:
                raise RuntimeError("Draft confirmation moved a session backwards")
            if confirmed.context_length < session.context_count:
                raise RuntimeError("Draft confirmation moved feature history backwards")
            session.token_count = confirmed.confirmed_length
            session.context_count = confirmed.context_length
            session.initial_prompt = None

    @torch.inference_mode()
    def _complete_proposal(
        self, staged: StagedProposal, output: DeviceFuture[torch.Tensor]
    ) -> None:
        """Publish ordered peer output with its producer-stream dependency for native consumers."""
        try:
            if self.failed:
                raise RuntimeError("the Draft binding has failed")
            execute = (
                staged.prepare_sent.result()
                if staged.prepare_sent is not None
                else False
            )
            if self.feature_capture is not None:
                # Feature sends wait on native layer events, not on sampling
                # metadata. The control binding owns the receiver throughout.
                with record_function_or_nullcontext("DT Target transfer"):
                    self.feature_capture.transfer(
                        staged.feature_rows, self.destination, self.nccl
                    )
                if execute:
                    self.nccl.send_device_inputs(
                        staged.device_inputs, staged.device_inputs_ready
                    )
                staged.features_transferred.set_result(None)
            requests = None if execute else self._confirmed_requests(staged)
            tokens = None
            log_q = None
            if self.nccl is not None:
                with (
                    torch.cuda.device(self.device),
                    torch.cuda.stream(self.result_stream),
                ):
                    tokens = torch.empty(
                        len(staged.request_ids),
                        self.k,
                        dtype=torch.int64,
                        device=self.device,
                    )
                    if self.draft_logits is not None:
                        log_q = torch.empty(
                            len(staged.request_ids),
                            self.k,
                            self.config.model_config.get_vocab_size(),
                            dtype=torch.float32,
                            device=self.device,
                        )
            sequence = self.sequence
            batch = (
                ExecuteBatch(sequence=sequence)
                if execute
                else ProposeBatch(sequence=sequence, requests=requests)
            )
            payload = {
                "operation": "execute" if execute else "propose",
                "payload": batch.model_dump(),
            }
            with record_function_or_nullcontext("DT Target proposal RPC"):
                if self.nccl is not None:
                    self.control.send(
                        json.dumps(payload, separators=(",", ":"), allow_nan=False)
                    )
                else:
                    result = self._exchange_control(payload)
                    if result["sequence"] != sequence:
                        raise RuntimeError(
                            "Draft returned a different proposal sequence"
                        )
            # The native Future records the producing stream. Consumers inherit
            # that dependency without waiting for the device on this IO thread.
            with torch.cuda.device(self.device), torch.cuda.stream(self.result_stream):
                if self.nccl is not None:
                    # Keep persistent receive work behind the Target's completed inputs.
                    self.result_stream.wait_event(staged.copy_done)
                    self.nccl.receive_output(tokens, log_q)
                    self.result_stream.wait_event(self.nccl.output_ready)
                else:
                    host_tokens = torch.tensor(
                        result["token_ids"],
                        dtype=torch.int64,
                        device="cpu",
                        pin_memory=True,
                    )
                    tokens = host_tokens.to(self.device, non_blocking=True)
                    if self.draft_logits is not None:
                        log_q = self.exchange.buffers["logits"][
                            : len(staged.request_ids)
                        ].to(self.device)
                if tokens.shape != (len(staged.request_ids), self.k):
                    raise RuntimeError("Draft returned a different candidate layout")
                if self.draft_logits is not None:
                    staged.indices.record_stream(self.result_stream)
                    scale = torch.as_tensor(
                        staged.temperatures, device=self.device
                    ).clamp_min(1e-20)
                    self.draft_logits[staged.indices] = log_q * scale[:, None, None]
                self.sequence += 1
                output.set_result(tokens)
            # Native consumers already own the device result. Execute ACKs carry
            # Draft-owned confirmation; ordinary proposals retain host fallback.
            if requests is not None:
                for request in requests:
                    session = self.sessions[request.request_id]
                    session.token_count = request.generation
                    session.context_count = request.context_start + request.feature_count
                    session.initial_prompt = None
            if self.nccl is not None:
                # Like native PP's side-stream result, GPU consumers can proceed
                # before CPU bookkeeping finishes. This ordered connection still
                # joins the engine acknowledgment before admitting another turn.
                result = self._receive_control()
                if execute:
                    self._apply_execute_result(staged, result, sequence)
                elif result["sequence"] != sequence:
                    raise RuntimeError("Draft returned a different proposal sequence")
        except BaseException as error:
            self.failed = True
            if self.nccl is not None:
                self.nccl.close(abort=True)
            if (
                staged.features_transferred is not None
                and not staged.features_transferred.done()
            ):
                staged.features_transferred.set_exception(error)
            if not output.done():
                if not isinstance(error, Exception):
                    failure = RuntimeError(
                        "The Draft proposal operation was interrupted"
                    )
                    failure.__cause__ = error
                    error = failure
                output.set_exception(error)
            staged.copy_done.synchronize()
            raise

    def remove_requests(self, request_ids: set[str]) -> None:
        """Join ordered peer cleanup before native request IDs or slots are reused."""
        cleanup = self.executor.submit(self._finish_requests, request_ids)
        try:
            cleanup.result(timeout=self.config.shutdown_timeout)
        except TimeoutError as error:
            self.failed = True
            raise RuntimeError(
                "Draft request cleanup exceeded the drain budget"
            ) from error

    def _finish_requests(self, request_ids: set[str]) -> None:
        """Release retained peer sessions after their preceding proposals complete."""
        active = [
            request_id
            for request_id in request_ids
            if request_id in self.sessions and self.sessions[request_id].token_count
        ]
        if active and self.control is not None and not self.failed:
            try:
                self._exchange_control(
                    {
                        "operation": "finish",
                        "payload": FinishRequests(request_ids=active).model_dump(),
                    }
                )
            except BaseException:
                self.failed = True
                raise
        for request_id in request_ids:
            self.sessions.pop(request_id, None)

    def close(self) -> None:
        """Join outstanding peer operations before native worker and transport teardown."""
        executor, self.executor = self.executor, None
        if executor is None:
            return
        try:
            executor.submit(self._close_peer).result()
        finally:
            executor.shutdown(wait=True)

    def _close_peer(self) -> None:
        """Close the remote owner before unregistering buffers that it can still write."""
        try:
            if self.control is not None and not self.failed:
                self._exchange_control({"operation": "close"})
        except BaseException:
            self.failed = True
            raise
        finally:
            try:
                if self.control is not None:
                    self.control_scope.__exit__(None, None, None)
                    self.control = None
            finally:
                try:
                    if self.nccl is not None:
                        self.nccl.close(
                            abort=self.failed or self.prepare_sent is not None
                        )
                finally:
                    try:
                        if self.feature_capture is not None:
                            self.feature_capture.close()
                    finally:
                        if self.exchange is not None:
                            self.exchange.close()

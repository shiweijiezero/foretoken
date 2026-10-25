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
from vllm.utils.platform_utils import is_uva_available
from vllm.v1.utils import record_function_or_nullcontext
from vllm.v1.worker.gpu.async_utils import async_copy_to_np, stream
from vllm.v1.worker.gpu.spec_decode.speculator import BaseSpeculator
from websockets.sync.client import connect

from .exchange import TensorExchange
from .protocol import (
    FinishRequests,
    OpenPeer,
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


@dataclass
class StagedProposal:
    """Retain a native batch's input storage until its ordered peer operation completes."""

    request_ids: list[str]
    indices: torch.Tensor
    metadata: np.ndarray
    token_ids: list[np.ndarray]
    token_offsets: list[int]
    features: torch.Tensor | None
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
        self.exchange = TensorExchange(settings.protocol)
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
        self.sessions.setdefault(request.req_id, ProposalSession(request.cache_salt))

    def init_cudagraph_manager(self, mode):
        """Keep Target graph ownership native; the remote Draft captures its own graphs."""

    def capture(self):
        """Remote work is outside the Target's CUDA graph capture."""

    def _open(self, features: torch.Tensor | None) -> None:
        """Retain the control stream that owns every native Draft session for this worker."""
        num_features = 0 if features is None else features.shape[1]
        hidden_size = self.config.model_config.get_hidden_size()
        dtype = str(self.config.model_config.dtype).removeprefix("torch.")
        if features is not None:
            hidden_size = features.shape[2]
            dtype = str(features.dtype).removeprefix("torch.")
            self.exchange.allocate(
                "features", [self.max_tokens, num_features, hidden_size], dtype
            )
        logits = None
        if self.draft_logits is not None:
            logits = self.exchange.allocate(
                "logits",
                [self.max_reqs, self.k, self.config.model_config.get_vocab_size()],
                "float32",
            )
        request = OpenPeer(
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
            logits=logits,
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
        except BaseException:
            self.control = None
            scope.__exit__(None, None, None)
            raise
        self.control_scope = scope

    def _exchange_control(self, payload):
        """Send one ordered peer operation and wait for its native completion acknowledgment."""
        self.control.send(json.dumps(payload, separators=(",", ":"), allow_nan=False))
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
    ) -> torch.Tensor | Future[torch.Tensor]:
        """Stage native inputs and return proposals now or when their peer operation completes."""
        if dummy_run:
            return self.draft_tokens[: input_batch.num_reqs].zero_()
        if self.failed:
            raise RuntimeError("the Draft binding has failed")
        features = None
        if not self.spec.uses_draft_model():
            features = torch.stack(aux_hidden_states or [last_hidden_states], dim=1)[
                : input_batch.num_tokens
            ]
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
            start = self.sessions[request_id].token_count
            token_offsets.append(start)
            if self.token_history_cpu is not None:
                # Native token history is pinned host memory accessed through UVA.
                # Read it after copy_done; CUDA's host-to-host copy would block here.
                # Pending proposals retain the request slot until their consumption.
                token_rows.append(self.token_history_cpu[slot, start:end])
            else:
                token_sources.append(states.all_token_ids.gpu[slot, start:end])
        copied_features = (
            torch.empty_like(features, device="cpu", pin_memory=True)
            if features is not None
            else None
        )
        copy_stream = self.runner.output_copy_stream
        copy_done = torch.cuda.Event(blocking=True)
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
                    if copied_features is not None:
                        copied_features.copy_(features, non_blocking=True)
                finally:
                    copy_done.record(copy_stream)
        except BaseException:
            # A partial batch still owns every source and destination until DMA ends.
            copy_done.synchronize()
            self.failed = True
            raise
        sampling = self.runner.sampler.sampling_states
        staged = StagedProposal(
            request_ids=list(input_batch.req_ids),
            indices=indices,
            metadata=copied_metadata,
            token_ids=token_rows,
            token_offsets=token_offsets,
            features=copied_features,
            query_start_loc=input_batch.query_start_loc_np.tolist(),
            temperatures=sampling.temperature.np[input_batch.idx_mapping_np],
            seeds=sampling.seeds.np[input_batch.idx_mapping_np],
            copy_done=copy_done,
            sources=[metadata, *token_sources]
            + ([features] if features is not None else []),
        )
        return self.executor.submit(self._complete_proposal, staged)

    @torch.inference_mode()
    def _complete_proposal(self, staged: StagedProposal) -> torch.Tensor:
        """Own ordered transfer and completion without retaining the Target execution thread."""
        try:
            staged.copy_done.synchronize()
            staged.sources.clear()
            if self.failed:
                raise RuntimeError("the Draft binding has failed")
            if self.control is None:
                self._open(staged.features)
            if staged.features is not None:
                self.exchange.buffers["features"][: len(staged.features)].copy_(
                    staged.features
                )
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
                if staged.features is not None:
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
            if self.destination is not None:
                with record_function_or_nullcontext("DT Target transfer"):
                    self.exchange.write(
                        "features", len(staged.features), self.destination
                    )
            batch = ProposeBatch(sequence=self.sequence, requests=requests)
            with record_function_or_nullcontext("DT Target proposal RPC"):
                result = self._exchange_control(
                    {"operation": "propose", "payload": batch.model_dump()}
                )
            if result["sequence"] != self.sequence:
                raise RuntimeError("Draft returned a different proposal sequence")
            # Complete device publication before fulfilling the CPU Future.
            # Its consumer can then apply these independent rows on the native stream.
            with torch.cuda.device(self.device), torch.cuda.stream(self.result_stream):
                tokens = torch.tensor(
                    result["token_ids"], dtype=torch.int64, device=self.device
                )
                if tokens.shape != (len(staged.request_ids), self.k):
                    raise RuntimeError("Draft returned a different candidate layout")
                if self.draft_logits is not None:
                    log_q = self.exchange.buffers["logits"][
                        : len(staged.request_ids)
                    ].to(self.device)
                    scale = torch.as_tensor(
                        staged.temperatures, device=self.device
                    ).clamp_min(1e-20)
                    self.draft_logits[staged.indices] = log_q * scale[:, None, None]
                self.result_stream.synchronize()
            self.sequence += 1
            for request in requests:
                session = self.sessions[request.request_id]
                session.token_count = request.generation
                session.context_count = request.context_start + request.feature_count
            return tokens
        except BaseException:
            self.failed = True
            raise

    def remove_requests(self, request_ids: set[str]) -> None:
        """Join ordered peer cleanup before native request IDs or slots are reused."""
        self.executor.submit(self._finish_requests, request_ids).result()

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
                self.exchange.close()

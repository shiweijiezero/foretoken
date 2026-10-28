# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Bind remote proposal IO to native Worker construction, execution and shutdown."""

from dataclasses import dataclass, field
from itertools import chain

import torch
import vllm
from foretoken_mctracer import Worker as MctracerWorker
from packaging.version import Version
from vllm.v1.worker import gpu_worker
from vllm.v1.worker.gpu import model_runner
from vllm.v1.worker.gpu.spec_decode.autoregressive.speculator import (
    AutoRegressiveSpeculator,
)
from vllm.v1.worker.gpu.spec_decode.speculator import BaseSpeculator

from .context import DraftContext
from .device_input import DeviceInputBatch, DeviceInputRow
from .exchange import TensorExchange
from .nccl import NcclPeer
from .protocol import PEER_PROTOCOL_VERSION, OpenPeer, PrepareBatch, Settings
from .runner import DraftModelRunner, DraftOutput, replace_binding
from .scheduler import DraftCachedRequests

if Version(vllm.__version__).release[:2] != (0, 31):
    raise ImportError("DT currently requires the vLLM 0.31 native adapter")
if not hasattr(BaseSpeculator, "prepare_target_forward"):
    raise ImportError("DT requires Foretoken's vLLM proposal-input lifecycle patch")


def validate_config(config):
    """Reject native execution modes whose state is not represented by the DT adapter."""
    parallel = config.parallel_config
    if parallel.world_size != 1 or parallel.data_parallel_size != 1:
        raise ValueError(
            "DT currently requires one native worker per Draft or Target instance"
        )
    if config.model_config.supports_multimodal_inputs or config.lora_config is not None:
        raise ValueError("DT currently supports text requests without LoRA")
    if config.watermark_config is not None:
        raise ValueError("DT does not yet transfer native watermarking state")
    spec = config.speculative_config
    if spec is not None:
        if spec.draft_parallel_config.world_size != 1:
            raise ValueError("DT currently requires a single-worker Draft")
        if spec.use_heterogeneous_vocab:
            raise ValueError("DT requires Draft and Target to share a token vocabulary")
        if spec.method not in ("draft_model", "eagle", "eagle3", "mtp"):
            raise ValueError(f"DT has no native proposal adapter for {spec.method}")
        if spec.enable_adaptive_verification:
            raise ValueError("DT requires a fixed native speculative-token count")
        if not spec.uses_draft_model() and config.cache_config.enable_prefix_caching:
            raise ValueError(
                "Neural DT requires Target features from the full computed prefix"
            )


class WorkerExtension:
    """Replace only the Target's proposer while leaving its native runner and scheduler intact."""

    def init_worker_extension(self):
        """Install remote proposal execution after device setup and before model loading."""
        from .proposer import RemoteProposer

        validate_config(self.vllm_config)
        settings = Settings.from_engine(self.vllm_config)
        if settings.role != "target" or self.vllm_config.speculative_config is None:
            raise ValueError(
                "The remote proposer requires a Target speculative configuration"
            )
        if not isinstance(self.model_runner, model_runner.GPUModelRunner):
            raise TypeError("DT requires the native V2 model runner")
        native = self.model_runner.speculator
        if (
            isinstance(native, AutoRegressiveSpeculator)
            and not native.advance_draft_positions
        ):
            raise ValueError(
                "This neural Draft requires Target KV, which DT does not transfer yet"
            )
        self.model_runner.speculator = RemoteProposer(self, settings)

    def destroy_worker_extension(self):
        """Close an initialized remote proposer before the native worker releases its device."""
        from .proposer import RemoteProposer

        runner = getattr(self, "model_runner", None)
        if runner is not None and isinstance(runner.speculator, RemoteProposer):
            runner.speculator.close()


@dataclass
class DraftPeer:
    """Retain one connection's transfer buffers and current native output batch."""

    settings: OpenPeer
    features: torch.Tensor | None = None
    channel: NcclPeer | None = None
    prepared: PrepareBatch | None = None
    output: DraftOutput | None = None
    device_inputs: DeviceInputBatch | None = None
    sessions: list[str] = field(default_factory=list)


class DraftWorker(gpu_worker.Worker):
    """Own native Draft turns and tensor transfers for each connected Target worker."""

    def init_device(self):
        """Select the thin Draft runner at the native worker's existing factory boundary."""
        validate_config(self.vllm_config)
        settings = Settings.from_engine(self.vllm_config)
        if settings.role != "draft":
            raise ValueError("The Draft worker requires the Draft role")
        with replace_binding(model_runner, "GPUModelRunner", DraftModelRunner):
            super().init_device()
        if not isinstance(self.model_runner, DraftModelRunner):
            raise TypeError("DT requires the native V2 model runner")
        native = self.model_runner.speculator
        if native is not None and (
            not isinstance(native, AutoRegressiveSpeculator)
            or not native.advance_draft_positions
        ):
            raise ValueError("DT requires an autoregressive Draft with private KV")
        self.dt_settings = settings
        self.dt_exchange = (
            TensorExchange(settings.protocol) if settings.protocol != "nccl" else None
        )
        self.dt_history_stream = torch.cuda.Stream(self.device)
        self.dt_peers: dict[str, DraftPeer] = {}

    def compile_or_warm_up_model(self):
        """Run native warmup independently of Target connections and retained requests."""
        self.model_runner.warming_up = True
        try:
            return super().compile_or_warm_up_model()
        finally:
            self.model_runner.warming_up = False

    def dt_requires_target_features(self):
        """Publish native feature requirements to the model-server startup owner."""
        return self.model_runner.context_features is not None

    def dt_open_peer(self, payload):
        """Allocate registered IO only after checking this worker's actual proposal layout."""
        peer = OpenPeer.model_validate(payload)
        if peer.protocol_version != PEER_PROTOCOL_VERSION:
            raise ValueError("Draft and Target peer protocol versions differ")
        if peer.peer_id in self.dt_peers:
            raise ValueError("DT peer already exists")
        runner = self.model_runner
        spec = self.vllm_config.speculative_config
        method = spec.method if spec is not None else "draft_model"
        if peer.method != method or peer.protocol != self.dt_settings.protocol:
            raise ValueError("Draft method or transport differs from Target")
        if peer.vocab_size != runner.vocab_size:
            raise ValueError("Draft and Target vocabularies differ")
        if spec is not None and peer.num_candidates != spec.num_speculative_tokens:
            raise ValueError("Draft and Target speculative-token counts differ")
        features = runner.context_features
        if features is None:
            if peer.num_features:
                raise ValueError("Ordinary Drafts do not consume Target features")
        elif (peer.num_features, peer.hidden_size) != tuple(
            features.shape[1:]
        ) or peer.feature_dtype != str(features.dtype).removeprefix("torch."):
            raise ValueError(
                "Target features differ from the native Draft input layout"
            )
        if (
            peer.return_probabilities
            and spec is not None
            and spec.draft_sample_method != "probabilistic"
        ):
            raise ValueError("Target requested q from a greedy neural Draft")
        if peer.logits is not None and (
            peer.logits.shape
            != [peer.max_requests, peer.num_candidates, peer.vocab_size]
            or peer.logits.dtype != "float32"
        ):
            raise ValueError("Target probability buffer has a different layout")
        state = DraftPeer(peer)
        self.dt_peers[peer.peer_id] = state
        try:
            if peer.protocol == "nccl":
                if peer.nccl_ids is None or peer.logits is not None:
                    raise ValueError(
                        "NCCL peers require native IDs and GPU result buffers"
                    )
                state.channel = NcclPeer(peer.nccl_ids, 1, self.device)
                if features is not None:
                    state.features = torch.empty(
                        peer.num_features,
                        peer.max_tokens,
                        peer.hidden_size,
                        dtype=features.dtype,
                        device=self.device,
                    )
                return None
            if peer.nccl_ids is not None or peer.return_probabilities != (
                peer.logits is not None
            ):
                raise ValueError(
                    "Mooncake peer result registration differs from its sampling mode"
                )
            if peer.logits is not None:
                self.dt_exchange.allocate(
                    f"{peer.peer_id}:logits", peer.logits.shape, "float32"
                )
            region = None
            if features is not None:
                name = f"{peer.peer_id}:features"
                region = self.dt_exchange.allocate(
                    name,
                    [peer.num_features, peer.max_tokens, peer.hidden_size],
                    peer.feature_dtype,
                )
                state.features = self.dt_exchange.buffers[name]
            return region.model_dump() if region is not None else None
        except BaseException:
            self.dt_close_peer(peer.peer_id, abort=True)
            raise

    def dt_connect_peer(self, peer_id):
        """Join native communicators after both roles have accepted their layouts."""
        self.dt_peers[peer_id].channel.connect()

    def dt_prepare(self, peer_id, payload):
        """Post native receives from announced shapes while the Target computes."""
        prepared = PrepareBatch.model_validate(payload)
        state = self._begin_prepare(peer_id, prepared)
        if state.features is not None:
            state.channel.receive_features(
                state.features, prepared.feature_rows, self.model_runner.main_stream
            )

    def _begin_prepare(self, peer_id, prepared):
        """Retire the preceding peer operation before preparing its next native input."""
        state = self.dt_peers[peer_id]
        if state.prepared is not None:
            if state.output is None or state.output.pending:
                raise RuntimeError(
                    "The preceding Draft batch has not produced all native outputs"
                )
            # The ordered peer connection has joined native turn completion.
            # Retire its bindings before receiving into the shared input buffer.
            for session in state.sessions:
                self.model_runner.outputs.pop(session, None)
                self.model_runner.device_inputs.pop(session, None)
                context = self.model_runner.contexts.get(session)
                if context is not None:
                    context.current = None
            state.sessions.clear()
            state.output = None
            state.device_inputs = None
        if (
            prepared.feature_rows > state.settings.max_tokens
            or prepared.num_requests > state.settings.max_requests
        ):
            raise ValueError("The announced batch exceeds the peer's input capacity")
        if state.channel is None:
            raise ValueError("Explicit receive preparation requires an NCCL peer")
        if state.features is None and prepared.feature_rows:
            raise ValueError("Ordinary Drafts do not receive Target features")
        state.prepared = prepared
        return state

    def dt_execute(self, peer_id, sequence, requests):
        """Bind a pre-admitted neural turn before Target device confirmation arrives."""
        state = self.dt_peers[peer_id]
        peer = state.settings
        if state.channel is None or not peer.num_features:
            raise ValueError("Device turns require a neural NCCL peer")
        num_tokens = sum(count for _, count, _, _ in requests)
        self._begin_prepare(
            peer_id,
            PrepareBatch(
                sequence=sequence,
                feature_rows=num_tokens,
                num_requests=len(requests),
            ),
        )
        packed = torch.empty(
            2 * num_tokens + 5 * len(requests),
            dtype=torch.int64,
            device=self.device,
        )
        received = DeviceInputBatch(
            sequence,
            packed,
            state.features,
            num_tokens,
            len(requests),
            state.channel.input_ready,
        )
        rows = []
        contexts = {}
        bindings = {}
        offset = 0
        for row, (session, count, upper_bound, initial) in enumerate(requests):
            if initial:
                if session in self.model_runner.contexts:
                    raise ValueError("Initial device turn already has feature history")
                contexts[session] = DraftContext()
                contexts[session].reserve_history(
                    count, (peer.num_features, peer.hidden_size), state.features.dtype
                )
            elif session not in self.model_runner.contexts:
                raise ValueError(
                    "Device turn requires a retained native feature prefix"
                )
            bindings[session] = DeviceInputRow(
                received, row, offset, count, upper_bound, initial
            )
            rows.append((session, 0, offset, count, peer.num_candidates))
            offset += count
        output = DraftOutput(peer, state.channel, rows)
        # Allocation can synchronize the device. Finish it before posting a
        # receive whose Target sender is still waiting for preparation's ACK.
        state.channel.receive_features(
            state.features, num_tokens, self.model_runner.main_stream
        )
        state.channel.receive_device_inputs(packed)
        state.device_inputs = received
        state.output = output
        state.sessions = [session for session, _, _, _ in requests]
        self.model_runner.contexts.update(contexts)
        self.model_runner.device_inputs.update(bindings)
        self.model_runner.outputs.update(
            (session, state.output) for session in state.output.pending
        )

    def _stage_features(self, session, peer_id, context_start, feature_offset, count):
        """Retain one received interval across native execution and KV recomputation."""
        context = self.model_runner.contexts.setdefault(session, DraftContext())
        # Native preemption can resubmit a request after the Target has reused
        # this peer's receive buffer. The retained history already owns this turn.
        if context.end == context_start + count:
            return
        state = self.dt_peers[peer_id]
        end = feature_offset + count
        available = (
            state.prepared.feature_rows
            if state.prepared is not None
            else state.settings.max_tokens
        )
        if end > available:
            raise ValueError("Target feature interval exceeds its received input")
        with torch.cuda.stream(self.model_runner.main_stream):
            if state.channel is not None:
                self.model_runner.main_stream.wait_event(state.channel.input_ready)
            context.append(
                context_start,
                state.features[:, feature_offset:end].movedim(0, 1),
                self.dt_history_stream,
            )

    def execute_model(self, scheduler_output):
        """Consume confirmed features before the native Worker executes their scheduled tokens."""
        cached = scheduler_output.scheduled_cached_reqs
        if (isinstance(cached, DraftCachedRequests) and cached.reserved_turns) or any(
            (request.sampling_params.extra_args or {}).get("dt_reserved_turn")
            for request in scheduler_output.scheduled_new_reqs
        ):
            return super().execute_model(scheduler_output)
        parameters = (
            request.sampling_params for request in scheduler_output.scheduled_new_reqs
        )
        if isinstance(cached, DraftCachedRequests):
            parameters = chain(
                parameters, (turn.sampling_params for turn in cached.turns.values())
            )
        for params in parameters:
            if params is not None and params.extra_args:
                features = params.extra_args.get("dt_features")
                if features is not None:
                    self._stage_features(params.extra_args["dt_session"], *features)
        return super().execute_model(scheduler_output)

    def dt_stage(self, peer_id, requests):
        """Retain confirmed inputs and bind GPU output rows before native batch admission."""
        state = self.dt_peers[peer_id]
        peer = state.settings
        if state.channel is not None:
            if state.prepared is None or state.prepared.num_requests != len(requests):
                raise ValueError("Draft request rows differ from the prepared transfer")
            state.output = DraftOutput(peer, state.channel, requests)
            state.sessions = [session for session, *_ in requests]
            self.model_runner.outputs.update(
                (session, state.output) for session in state.output.pending
            )
        for session, context_start, feature_offset, feature_count, count in requests:
            if peer.num_features:
                self._stage_features(
                    session, peer_id, context_start, feature_offset, feature_count
                )
            if count and peer.return_probabilities and state.channel is None:
                self.model_runner.probabilities[session] = []
        if state.output is not None and not state.output.pending:
            state.output.publish()

    def dt_publish_probabilities(self, peer_id, sessions):
        """Write actual normalized proposal laws before acknowledging the proposal batch."""
        peer = self.dt_peers[peer_id].settings
        if peer.logits is None:
            return
        output = self.dt_exchange.buffers[f"{peer_id}:logits"]
        for row, proposal in enumerate(sessions):
            output[row].zero_()
            if proposal is None:
                continue
            session, count = proposal
            probabilities = self.model_runner.probabilities[session]
            if len(probabilities) != count:
                raise RuntimeError(
                    "Native Draft returned an incomplete probability sequence"
                )
            output[row, :count].copy_(torch.stack(probabilities))
            probabilities.clear()
        self.dt_exchange.write(f"{peer_id}:logits", len(sessions), peer.logits)

    def dt_wait_execution(self):
        """Fence native consumers and retained history before releasing session data."""
        self.model_runner.main_stream.synchronize()
        self.dt_history_stream.synchronize()

    def dt_forget_sessions(self, sessions):
        """Release retained data after the engine has joined cancellation and pending proposals."""
        for session in sessions:
            self.model_runner.contexts.pop(session, None)
            self.model_runner.probabilities.pop(session, None)
            self.model_runner.outputs.pop(session, None)
            self.model_runner.device_inputs.pop(session, None)

    def dt_close_peer(self, peer_id, abort=False):
        """Close native IO before releasing buffers retained by this control connection."""
        state = self.dt_peers.pop(peer_id, None)
        if state is not None and state.channel is not None:
            pending = state.prepared is not None and (
                state.output is None or bool(state.output.pending)
            )
            state.channel.close(abort=abort or pending)
        if self.dt_exchange is not None:
            self.dt_exchange.release(f"{peer_id}:features")
            self.dt_exchange.release(f"{peer_id}:logits")

    def shutdown(self):
        """Abort unfinished receives before native worker teardown waits on the device."""
        try:
            for peer_id in list(getattr(self, "dt_peers", ())):
                self.dt_close_peer(peer_id, abort=True)
            if hasattr(self, "dt_exchange"):
                self.dt_wait_execution()
                if self.dt_exchange is not None:
                    self.dt_exchange.close()
        finally:
            super().shutdown()


class MctracerDraftWorker(DraftWorker, MctracerWorker):
    """Combine Draft execution with the existing mcTracer worker's initialization and RPC."""

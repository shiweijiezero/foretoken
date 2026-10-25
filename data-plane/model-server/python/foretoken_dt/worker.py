# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Bind remote proposal IO to native Worker construction, execution and shutdown."""

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
from .exchange import TensorExchange
from .protocol import OpenPeer, Settings
from .runner import DraftModelRunner, replace_binding
from .scheduler import DraftCachedRequests

if Version(vllm.__version__).release[:2] != (0, 31):
    raise ImportError("DT currently requires the vLLM 0.31 native adapter")
if not hasattr(BaseSpeculator, "add_request"):
    raise ImportError("DT requires Foretoken's vLLM worker lifecycle patch")


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


class DraftWorker(gpu_worker.Worker):
    """Own native Draft turns and Mooncake buffers for every connected Target worker."""

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
        self.dt_exchange = TensorExchange(settings.protocol)
        self.dt_peers = {}

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
        if peer.logits is not None:
            if spec is not None and spec.draft_sample_method != "probabilistic":
                raise ValueError("Target requested q from a greedy neural Draft")
            if peer.logits.shape != [
                peer.max_requests,
                peer.num_candidates,
                peer.vocab_size,
            ] or (peer.logits.dtype != "float32"):
                raise ValueError("Target probability buffer has a different layout")
        self.dt_peers[peer.peer_id] = peer
        try:
            if peer.logits is not None:
                self.dt_exchange.allocate(
                    f"{peer.peer_id}:logits", peer.logits.shape, "float32"
                )
            region = None
            if features is not None:
                region = self.dt_exchange.allocate(
                    f"{peer.peer_id}:features",
                    [peer.max_tokens, peer.num_features, peer.hidden_size],
                    peer.feature_dtype,
                )
            return region.model_dump() if region is not None else None
        except BaseException:
            self.dt_close_peer(peer.peer_id)
            raise

    def _stage_features(self, session, peer_id, context_start, feature_offset, count):
        """Retain one received interval across native execution and KV recomputation."""
        context = self.model_runner.contexts.setdefault(session, DraftContext())
        # Native preemption can resubmit a request after the Target has reused
        # this peer's receive buffer. The retained history already owns this turn.
        if context.end == context_start + count:
            return
        end = feature_offset + count
        if end > self.dt_peers[peer_id].max_tokens:
            raise ValueError("Target feature interval exceeds its registered input")
        context.append(
            context_start,
            self.dt_exchange.buffers[f"{peer_id}:features"][feature_offset:end],
        )

    def execute_model(self, scheduler_output):
        """Consume confirmed features before the native Worker executes their scheduled tokens."""
        parameters = (
            request.sampling_params for request in scheduler_output.scheduled_new_reqs
        )
        cached = scheduler_output.scheduled_cached_reqs
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
        """Retain input-only batches and initialize probability capture before native admission."""
        peer = self.dt_peers[peer_id]
        for session, context_start, feature_offset, feature_count, generate in requests:
            if peer.num_features:
                self._stage_features(
                    session, peer_id, context_start, feature_offset, feature_count
                )
            if generate and peer.logits is not None:
                self.model_runner.probabilities[session] = []

    def dt_publish_probabilities(self, peer_id, sessions):
        """Write actual normalized proposal laws before acknowledging the proposal batch."""
        peer = self.dt_peers[peer_id]
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
        """Fence preceding serialized native computation before the session owner releases data."""
        self.model_runner.main_stream.synchronize()

    def dt_forget_sessions(self, sessions):
        """Release retained data after the engine has joined cancellation and pending proposals."""
        for session in sessions:
            self.model_runner.contexts.pop(session, None)
            self.model_runner.probabilities.pop(session, None)

    def dt_close_peer(self, peer_id):
        """Release a peer after its Target and native requests have confirmed completed writes."""
        self.dt_exchange.release(f"{peer_id}:features")
        self.dt_exchange.release(f"{peer_id}:logits")
        self.dt_peers.pop(peer_id, None)

    def shutdown(self):
        """Stop device work before closing the transport's retained allocations."""
        try:
            if hasattr(self, "dt_exchange"):
                self.dt_wait_execution()
                self.dt_exchange.close()
        finally:
            super().shutdown()


class MctracerDraftWorker(DraftWorker, MctracerWorker):
    """Combine Draft execution with the existing mcTracer worker's initialization and RPC."""

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Keep Target execution in vLLM and delegate EAGLE proposals to one Draft worker."""

from typing import Any

import torch
from vllm.config import VllmConfig
from vllm.config.compilation import CUDAGraphMode
from vllm.model_executor.layers.vocab_parallel_embedding import VocabParallelEmbedding
from vllm.v1.core.sched.output import GrammarOutput, SchedulerOutput
from vllm.v1.outputs import AsyncModelRunnerOutput, DraftTokenIds, ModelRunnerOutput
from vllm.v1.worker.gpu.input_batch import InputBatch
from vllm.v1.worker.gpu.spec_decode.eagle.eagle3_utils import (
    get_eagle3_aux_layers_from_config,
)
from vllm.v1.worker.gpu.spec_decode.eagle.utils import get_target_lm_head
from vllm.v1.worker.gpu.spec_decode.speculator import BaseSpeculator
from vllm.v1.worker.gpu_worker import Worker

from foretoken_dt.transport import Client, TensorLayout


def _aux_tensors(
    states: list[torch.Tensor], num_tokens: int
) -> dict[str, torch.Tensor]:
    """Select the unpadded EAGLE3 feature rows in native layer order for transmission."""
    return {
        f"aux_hidden_states_{i}": value[:num_tokens] for i, value in enumerate(states)
    }


def _known_inputs(
    batch: InputBatch,
    next_prefill_tokens: torch.Tensor,
    temperature: torch.Tensor,
    seeds: torch.Tensor,
) -> dict[str, torch.Tensor]:
    """Select the current forward's inputs that Target sampling leaves unchanged."""
    indices = batch.idx_mapping
    return {
        "input_ids": batch.input_ids[: batch.num_tokens],
        "positions": batch.positions[: batch.num_tokens],
        "is_padding": batch.is_padding[: batch.num_tokens],
        "seq_lens": batch.seq_lens[: batch.num_reqs],
        "next_prefill_tokens": next_prefill_tokens[:, indices],
        "temperature": temperature[indices],
        "seeds": seeds[indices],
    }


def _draft_engine_args(config: VllmConfig) -> dict[str, Any]:
    """Describe the Draft process using explicit, JSON-compatible engine inputs."""
    model = config.model_config
    spec = config.speculative_config
    compilation = config.compilation_config
    assert spec is not None
    return {
        "model": model.model,
        "revision": model.revision,
        "code_revision": model.code_revision,
        "hf_config_path": model.hf_config_path,
        "config_format": model.config_format,
        "trust_remote_code": model.trust_remote_code,
        "hf_overrides": model.hf_overrides,
        "dtype": str(model.dtype).removeprefix("torch."),
        "max_model_len": model.max_model_len,
        "max_num_seqs": config.scheduler_config.max_num_seqs,
        "max_num_batched_tokens": config.scheduler_config.max_num_batched_tokens,
        "enable_chunked_prefill": config.scheduler_config.enable_chunked_prefill,
        "enable_prefix_caching": False,
        "async_scheduling": False,
        "enforce_eager": model.enforce_eager,
        "skip_tokenizer_init": True,
        "seed": model.seed,
        # Forward compiler choices, not Target's populated layer registry or
        # compiled objects. Draft resolves support against its own attention backend.
        "compilation_config": {
            "mode": compilation.mode.value,
            "backend": compilation.backend,
            "cudagraph_mode": compilation.cudagraph_mode.name,
            "cudagraph_capture_sizes": compilation.cudagraph_capture_sizes,
            "max_cudagraph_capture_size": compilation.max_cudagraph_capture_size,
            "compile_sizes": compilation.compile_sizes,
            "compile_ranges_endpoints": compilation.compile_ranges_endpoints,
            "splitting_ops": compilation.splitting_ops,
            "use_inductor_graph_partition": compilation.use_inductor_graph_partition,
        },
        "speculative_config": {
            "model": spec.model,
            "revision": spec.draft_model_config.revision,
            "code_revision": spec.code_revision,
            "method": spec.method,
            "num_speculative_tokens": spec.num_speculative_tokens,
            "draft_sample_method": "greedy",
            "use_local_argmax_reduction": spec.use_local_argmax_reduction,
        },
    }


def _validate_config(config: VllmConfig) -> None:
    """Reject modes whose hidden-state or request lifecycle is not transported."""
    spec = config.speculative_config
    if spec is None or spec.method not in ("eagle", "eagle3"):
        raise ValueError("DT requires speculative method eagle or eagle3")
    parallel = config.parallel_config
    if any(
        size != 1
        for size in (
            parallel.tensor_parallel_size,
            parallel.pipeline_parallel_size,
            parallel.data_parallel_size,
            parallel.decode_context_parallel_size,
            parallel.prefill_context_parallel_size,
        )
    ):
        raise ValueError("DT currently requires one Target GPU and one Draft GPU")
    if config.scheduler_config.async_scheduling:
        raise ValueError("DT currently requires --no-async-scheduling")
    if config.cache_config.enable_prefix_caching:
        raise ValueError(
            "DT requires --no-enable-prefix-caching to receive every feature"
        )
    if config.model_config.is_multimodal_model or config.lora_config is not None:
        raise ValueError("DT currently supports text models without LoRA")
    if config.kv_transfer_config is not None:
        raise ValueError(
            "DT cannot reconstruct features from a transferred Target KV cache"
        )
    if config.model_config.quantization or spec.draft_model_config.quantization:
        raise ValueError("DT currently requires unquantized Target and Draft weights")
    if spec.draft_sample_method != "greedy":
        raise ValueError("DT currently supports greedy Draft proposals")
    if spec.parallel_drafting or spec.uses_dynamic_speculative_decoding():
        raise ValueError("DT currently requires autoregressive drafting with fixed K")
    if config.cache_config.cache_dtype != "auto" or spec.kv_cache_dtype not in (
        None,
        "auto",
    ):
        raise ValueError("DT currently requires KV cache dtype auto on both workers")
    if (
        config.weight_transfer_config is not None
        or config.model_config.enable_sleep_mode
    ):
        raise ValueError("DT does not support online weight updates or sleep mode")
    if spec.max_model_len not in (None, config.model_config.max_model_len):
        raise ValueError("DT requires the same maximum context length on both workers")
    if callable(config.model_config.hf_overrides):
        raise TypeError("DT requires JSON hf_overrides, not a Python callable")


class RemoteEagleSpeculator(BaseSpeculator):
    """Own the Target connection and order native consumers after remote candidates."""

    supports_mm_inputs = False
    draft_logits = None  # Greedy Draft proposals have a one-hot proposal distribution.

    def __init__(self, config: VllmConfig, device: torch.device):
        self.config = config
        self.device = device
        options = config.additional_config["foretoken_dt"]
        self.transport = options.get("transport", "nccl")
        if self.transport not in ("nccl", "tcp"):
            raise ValueError("DT transport must be nccl or tcp")
        self.early_feature_send = options.get("early_feature_send", False)
        if not isinstance(self.early_feature_send, bool):
            raise TypeError("DT early_feature_send must be a boolean")
        if self.early_feature_send and (
            self.transport != "nccl" or config.speculative_config.method != "eagle3"
        ):
            raise ValueError("DT early_feature_send requires EAGLE3 with NCCL")
        self.feedback_tail_layout: TensorLayout | None = None
        self.feature_batch: InputBatch | None = None
        self.prefill_batch: tuple[InputBatch, bool] | None = None
        self.prefill_hidden_layout: TensorLayout | None = None
        self.aux_hidden_state_count = 0
        self.structured_requests: set[str] = set()
        self.client = Client(
            options["endpoint"], int(options.get("timeout_seconds", 300) * 1000)
        )
        self.num_speculative_steps = config.num_speculative_tokens

    @torch.inference_mode()
    def start(self, model: torch.nn.Module) -> None:
        """Bootstrap Draft once with configuration and the shared vocabulary weights."""
        language_model = (
            model.get_language_model()
            if hasattr(model, "get_language_model")
            else model
        )
        inner = language_model.model
        embedding = getattr(inner, "embed_tokens", None)
        if embedding is None:
            embedding = getattr(inner, "embedding", None)
        head = get_target_lm_head(model, language_model)
        if not isinstance(embedding, VocabParallelEmbedding) or not isinstance(
            head, VocabParallelEmbedding
        ):
            raise TypeError("DT requires native vLLM vocabulary embedding and LM head")
        if getattr(head, "bias", None) is not None:
            raise ValueError("DT does not transport an LM-head bias")
        spec = self.config.speculative_config
        assert spec is not None
        aux_layers = ()
        if spec.method == "eagle3":
            aux_layers = (
                get_eagle3_aux_layers_from_config(spec)
                or model.get_eagle3_default_aux_hidden_state_layers()
            )
        if self.transport == "nccl":
            self.client.enable_nccl(self.device, split_requests=self.early_feature_send)
        self.aux_hidden_state_count = len(aux_layers)
        self.client.request(
            {
                "operation": "init",
                "engine_args": _draft_engine_args(self.config),
                "aux_hidden_state_count": len(aux_layers),
            },
            {"embed_tokens": embedding.weight, "lm_head": head.weight},
        )

    def init_cudagraph_manager(self, cudagraph_mode: CUDAGraphMode) -> None:
        """Target graph setup has no local Draft model to capture."""

    def capture(self) -> None:
        """Draft execution belongs to its remote process, outside Target graphs."""

    def _full_decode(self, batch: InputBatch) -> bool:
        """Identify decode batches where every request has its full K candidates."""
        return (
            batch.num_reqs > 0
            and not batch.has_prefill
            and batch.num_tokens == batch.num_reqs * (self.num_speculative_steps + 1)
            and batch.num_draft_tokens == batch.num_reqs * self.num_speculative_steps
        )

    def _proposal_metadata(self, batch: InputBatch, aux_count: int) -> dict[str, Any]:
        """Describe the rows and choose the reply mode before sending either input group."""
        metadata = {
            "operation": "propose",
            "req_ids": batch.req_ids,
            "num_scheduled_tokens": batch.num_scheduled_tokens.tolist(),
            "query_start_loc": batch.query_start_loc_np[: batch.num_reqs + 1].tolist(),
            "seq_lens_cpu_upper_bound": batch.seq_lens_cpu_upper_bound[
                : batch.num_reqs
            ].tolist(),
            "num_computed_tokens": batch.num_computed_tokens_np.tolist(),
            "prefill_len": batch.prefill_len_np.tolist(),
            "num_computed_prefill_tokens": batch.num_computed_prefill_tokens_np.tolist(),
            "aux_hidden_state_count": aux_count,
        }
        if (
            self.client.gpu_only_replies
            and not batch.has_structured_output_reqs
            and self._full_decode(batch)
        ):
            metadata["gpu_only_reply"] = True
        elif (
            self.client.prefill_without_reply
            and batch.has_prefill
            and self.structured_requests.isdisjoint(batch.req_ids)
            and (
                batch.num_computed_prefill_tokens_np + batch.num_scheduled_tokens
                < batch.prefill_len_np
            ).all()
        ):
            metadata["operation"] = "prefill"
        return metadata

    @torch.inference_mode()
    def prepare_prefill(
        self,
        batch: InputBatch,
        next_prefill_tokens: torch.Tensor,
        temperature: torch.Tensor,
        seeds: torch.Tensor,
    ) -> None:
        """Send known prefill or mixed-batch inputs before native Target forward."""
        if not (
            self.client.prefill_prepare
            and self.prefill_hidden_layout is not None
            and self.feedback_tail_layout is not None
            and batch.has_prefill
            and not batch.has_structured_output_reqs
            and self.structured_requests.isdisjoint(batch.req_ids)
        ):
            return
        metadata = self._proposal_metadata(batch, self.aux_hidden_state_count)
        hidden_layout = {
            name: ((batch.num_tokens, *tail), dtype)
            for name, (tail, dtype) in self.prefill_hidden_layout.items()
        }
        self.client.begin_prefill(
            metadata,
            _known_inputs(batch, next_prefill_tokens, temperature, seeds),
            hidden_layout,
            {
                name: ((batch.num_reqs, *tail), dtype)
                for name, (tail, dtype) in self.feedback_tail_layout.items()
            },
        )
        self.prefill_batch = (batch, metadata["operation"] == "propose")

    @torch.inference_mode()
    def send_features(
        self,
        batch: InputBatch,
        states: list[torch.Tensor] | None,
        next_prefill_tokens: torch.Tensor,
        temperature: torch.Tensor,
        seeds: torch.Tensor,
        last_hidden_states: torch.Tensor | None = None,
    ) -> None:
        """Start the proposal on the communication stream before Target samples."""
        if self.prefill_batch is not None:
            if self.prefill_batch[0] is not batch:
                raise RuntimeError("DT hidden states belong to a different prefill")
            if states:
                hidden = _aux_tensors(states, batch.num_tokens)
            else:
                assert last_hidden_states is not None
                hidden = {"last_hidden_states": last_hidden_states[: batch.num_tokens]}
            self.client.send_prefill_features(hidden)
            return
        if (
            self.early_feature_send
            and states
            and self.feedback_tail_layout is not None
            and not batch.has_structured_output_reqs
            and self._full_decode(batch)
        ):
            known = _aux_tensors(states, batch.num_tokens)
            known.update(_known_inputs(batch, next_prefill_tokens, temperature, seeds))
            self.client.begin_request(
                self._proposal_metadata(batch, len(states)),
                known,
                {
                    name: ((batch.num_reqs, *tail), dtype)
                    for name, (tail, dtype) in self.feedback_tail_layout.items()
                },
            )
            self.feature_batch = batch

    @torch.inference_mode()
    def propose(
        self,
        input_batch: InputBatch,
        attn_metadata: dict[str, Any],
        slot_mappings: dict[str, torch.Tensor],
        last_hidden_states: torch.Tensor,
        aux_hidden_states: list[torch.Tensor] | None,
        num_sampled: torch.Tensor,
        num_rejected: torch.Tensor,
        last_sampled: torch.Tensor,
        next_prefill_tokens: torch.Tensor,
        temperature: torch.Tensor,
        seeds: torch.Tensor,
        num_tokens_across_dp: torch.Tensor | None = None,
        dummy_run: bool = False,
        skip_attn_for_dummy_run: bool = False,
        mm_inputs: tuple[list[torch.Tensor], torch.Tensor] | None = None,
        is_profile: bool = False,
    ) -> torch.Tensor:
        """Submit this batch and return next candidates with native stream dependencies."""
        batch = input_batch
        if self.prefill_hidden_layout is None:
            # Native profile/dummy runs provide actual feature dtypes and tail
            # shapes before the first request. Wire row counts follow each batch.
            hidden = (
                _aux_tensors(aux_hidden_states, batch.num_tokens)
                if aux_hidden_states
                else {"last_hidden_states": last_hidden_states}
            )
            self.prefill_hidden_layout = {
                name: (tuple(value.shape[1:]), value.dtype)
                for name, value in hidden.items()
            }
            self.feedback_tail_layout = {
                "num_sampled": (tuple(num_sampled.shape[1:]), num_sampled.dtype),
                "num_rejected": (tuple(num_rejected.shape[1:]), num_rejected.dtype),
                "last_sampled": (tuple(last_sampled.shape[1:]), last_sampled.dtype),
            }
        if dummy_run or is_profile:
            return torch.zeros(
                (batch.num_reqs, self.num_speculative_steps),
                dtype=torch.int64,
                device=self.device,
            )
        # Physical block IDs and CUDA-graph padding are local to each worker.
        # Rejected positions inside each real query stay present for native EAGLE.
        tensors = {
            "num_sampled": num_sampled[: batch.num_reqs],
            "num_rejected": num_rejected[: batch.num_reqs],
            "last_sampled": last_sampled[batch.idx_mapping],
        }
        if self.transport == "nccl":
            # The fixed K reply can be received before Draft's control response.
            # Native scatter and draft-token handling wait on its GPU dependency;
            # the current AsyncOutput copy was already queued before this proposal.
            candidates = torch.empty(
                (batch.num_reqs, self.num_speculative_steps),
                dtype=torch.int64,
                device=self.device,
            )
            if self.prefill_batch is not None:
                prepared_batch, has_reply = self.prefill_batch
                if prepared_batch is not batch:
                    raise RuntimeError("DT feedback belongs to a different prefill")
                self.client.finish_prefill(
                    tensors, {"draft_tokens": candidates} if has_reply else None
                )
                self.prefill_batch = None
                return candidates if has_reply else candidates.zero_()
            if self.feature_batch is not None:
                if self.feature_batch is not batch:
                    raise RuntimeError(
                        "DT feedback belongs to a different feature batch"
                    )
                self.client.finish_request(
                    tensors, reply_tensors={"draft_tokens": candidates}
                )
                self.feature_batch = None
                return candidates
        tensors.update(_known_inputs(batch, next_prefill_tokens, temperature, seeds))
        metadata = self._proposal_metadata(batch, len(aux_hidden_states or []))
        if aux_hidden_states:
            tensors.update(_aux_tensors(aux_hidden_states, batch.num_tokens))
        else:
            tensors["last_hidden_states"] = last_hidden_states[: batch.num_tokens]
        if self.transport == "nccl":
            if metadata["operation"] == "prefill":
                # vLLM ignores candidates for an unfinished prefill, but still
                # scatters the proposer's result into its fixed-width buffer.
                self.client.send_request(metadata, tensors)
                return candidates.zero_()
            self.client.send_request(
                metadata, tensors, reply_tensors={"draft_tokens": candidates}
            )
            return candidates
        reply = self.client.request(metadata, tensors)
        candidates = reply.tensors["draft_tokens"]
        if candidates.shape != (batch.num_reqs, self.num_speculative_steps):
            raise ValueError("Draft returned a candidate batch with the wrong shape")
        return candidates.to(device=self.device, dtype=torch.int64)

    def free(self, req_ids: set[str], *, wait: bool = True) -> None:
        """Order remote release before later proposals, confirming completion when idle."""
        if req_ids:
            metadata = {"operation": "free", "req_ids": sorted(req_ids)}
            if not wait and self.client.free_without_reply:
                metadata["free_without_reply"] = True
                self.client.send_request(metadata)
            else:
                self.client.request(metadata)

    def finish_proposal(self) -> None:
        """Consume any pending control reply before further request mutations."""
        self.client.receive_pending()

    def close(self) -> None:
        """Release the Target socket; the Draft service owns its process lifetime."""
        self.client.close()


class _CheckedOutput(AsyncModelRunnerOutput):
    """Publish native output only after checking for an aborted candidate receive."""

    def __init__(self, native: AsyncModelRunnerOutput, client: Client):
        self.native = native
        self.client = client

    def get_output(self) -> ModelRunnerOutput:
        """Retain the native D2H wait and surface a latched failure before delivery."""
        try:
            return self.native.get_output()
        finally:
            self.client.check_error()


class TargetWorker(Worker):
    """Opt-in vLLM worker replacing only MRv2's local EAGLE proposer."""

    def init_device(self) -> None:
        """Install the remote proposer before native model loading allocates Draft weights."""
        _validate_config(self.vllm_config)
        super().init_device()
        if not self.use_v2_model_runner:
            raise ValueError("DT requires VLLM_USE_V2_MODEL_RUNNER=1")
        self.remote_draft = RemoteEagleSpeculator(self.vllm_config, self.device)
        self.model_runner.speculator = self.remote_draft
        native_prepare_inputs = self.model_runner.prepare_inputs

        def prepare_inputs(scheduler_output, batch_req_state, batch_desc):
            """Notify Draft after native input assembly and before any Target forward."""
            batch = native_prepare_inputs(scheduler_output, batch_req_state, batch_desc)
            if batch.has_prefill:
                runner = self.model_runner
                self.remote_draft.prepare_prefill(
                    batch,
                    runner.req_states.next_prefill_tokens,
                    runner.sampler.sampling_states.temperature.gpu,
                    runner.sampler.sampling_states.seeds.gpu,
                )
            return batch

        self.model_runner.prepare_inputs = prepare_inputs

    def load_model(self, *, load_dummy_weights: bool = False) -> None:
        """Load the Target normally, then initialize the independent Draft worker."""
        super().load_model(load_dummy_weights=load_dummy_weights)
        self.remote_draft.start(self.model_runner.get_model())

    def execute_model(self, scheduler_output: SchedulerOutput):
        """Retire Draft state, run Target and submit available features before sampling."""
        self.remote_draft.finish_proposal()
        if (
            scheduler_output.finished_req_ids
            or scheduler_output.preempted_req_ids
            or scheduler_output.scheduled_new_reqs
        ):
            retired = set(scheduler_output.finished_req_ids)
            retired.update(scheduler_output.preempted_req_ids or ())
            if retired:
                self.remote_draft.structured_requests.difference_update(retired)
            for request in scheduler_output.scheduled_new_reqs:
                if request.num_computed_tokens != 0:
                    raise ValueError(
                        "A new DT request must replay its full feature prefix"
                    )
                if request.prompt_embeds is not None:
                    raise ValueError("DT currently requires token-ID prompts")
                retired.add(request.req_id)
                # The scheduler's per-step structured-output flag is false for
                # unfinished prefills, including requests with an output grammar.
                if (
                    request.sampling_params
                    and request.sampling_params.structured_outputs
                ):
                    self.remote_draft.structured_requests.add(request.req_id)
                else:
                    self.remote_draft.structured_requests.discard(request.req_id)
            self.remote_draft.free(
                retired, wait=not scheduler_output.total_num_scheduled_tokens
            )
        try:
            result = super().execute_model(scheduler_output)
            if result is None and (
                self.remote_draft.prefill_batch is not None
                or self.remote_draft.early_feature_send
            ):
                state = self.model_runner.execute_model_state
                if state is not None:
                    runner = self.model_runner
                    self.remote_draft.send_features(
                        state.input_batch,
                        state.aux_hidden_states,
                        runner.req_states.next_prefill_tokens,
                        runner.sampler.sampling_states.temperature.gpu,
                        runner.sampler.sampling_states.seeds.gpu,
                        state.hidden_states,
                    )
            return result
        except Exception:
            if self.remote_draft.prefill_batch is not None:
                self.remote_draft.close()
            raise

    def sample_tokens(self, grammar_output: GrammarOutput | None):
        """Complete native sampling or abort an early transfer whose feedback cannot follow."""
        try:
            output = super().sample_tokens(grammar_output)
            client = self.remote_draft.client
            if client.gpu_only_replies:
                if isinstance(output, AsyncModelRunnerOutput):
                    return _CheckedOutput(output, client)
                client.check_error()
            return output
        except Exception:
            if (
                self.remote_draft.feature_batch is not None
                or self.remote_draft.prefill_batch is not None
            ):
                self.remote_draft.close()
            raise

    def take_draft_token_ids(self) -> DraftTokenIds | None:
        """Finish the reply at native post-step candidate collection.

        EngineCoreProc queues current outputs before this post-step call. The
        reply is consumed even for a just-finished request; no outstanding control
        transaction can leak into the next scheduled batch.
        """
        self.remote_draft.finish_proposal()
        return super().take_draft_token_ids()

    def shutdown(self) -> None:
        """Close transport locally before vLLM releases Target CUDA resources."""
        if remote := getattr(self, "remote_draft", None):
            remote.close()
        super().shutdown()

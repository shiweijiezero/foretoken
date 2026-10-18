# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""MRV2 adaptations for external candidates and Worker-owned distributions."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import torch
from vllm.v1.worker.gpu import model_runner
from vllm.v1.worker.gpu.sample.sampler import Sampler


@contextmanager
def replace_binding(module: Any, name: str, replacement: Any):
    """Restore a native binding after one synchronous Worker operation."""
    original = getattr(module, name)
    setattr(module, name, replacement)
    try:
        yield
    finally:
        setattr(module, name, original)


class _ProposalSampler(Sampler):
    """Retain the native sampler's actual distribution for a Draft observer."""

    capture_proposals = False

    def sample(self, *args, **kwargs):
        """Retain the native sampler's actual processed distribution for Worker export."""
        # RoleService supplies an independent seed for every ordinary Draft
        # request. Native seeded sampling materializes top-k/top-p logits rather
        # than taking the fused FlashInfer path, so q matches the actual draw.
        sampled, processed = super().sample(*args, **kwargs)
        if self.capture_proposals:
            self._proposal_logits = processed
        return sampled, processed

    def __call__(self, logits, input_batch):
        """Publish processed logits alongside the native mutable sampler output."""
        output = super().__call__(logits, input_batch)
        if self.capture_proposals:
            output.processed_logits = self._proposal_logits
            self._proposal_logits = None
        return output


class _ExternalDraftTokens:
    """External candidates never publish locally generated draft-token output."""

    def set_draft_tokens(self, input_batch, draft_tokens):
        """Leave proposal ownership with the remote Draft role."""

    def get_draft_tokens(self):
        """Return no local proposal to EngineCore after Target verification."""


class GPUModelRunner(model_runner.GPUModelRunner):
    """Reuse MRV2 execution while binding external proposals to persistent slots."""

    def __init__(self, vllm_config, device):
        self.external_speculation_io = None
        self.warming_up = False
        speculative = vllm_config.speculative_config
        self.context_role = vllm_config.additional_config.get(
            "foretoken_dt_context_role"
        )
        self.context_batch = None
        self.context_requests = {}
        self.external_speculation = speculative is not None and (
            speculative.method == "external" or self.context_role == "target"
        )
        if self.external_speculation:
            # MRV2 has no external-speculator factory. Retain its speculative
            # buffers and rejection sampler, without constructing a local model.
            factory = lambda *_: None
            if self.context_role:
                from .context import TargetContext

                factory = TargetContext
            with replace_binding(model_runner, "init_speculator", factory):
                super().__init__(vllm_config, device)
            self.draft_tokens_handler = _ExternalDraftTokens()
        else:
            super().__init__(vllm_config, device)
            if self.context_role == "draft":
                self.draft_tokens_handler = _ExternalDraftTokens()

    def load_model(self, *args, **kwargs):
        """Construct the proposal-aware sampler through native model loading."""
        with replace_binding(model_runner, "Sampler", _ProposalSampler):
            if self.context_role == "draft":
                from .context import SharedTargetLoader

                factory = model_runner.get_model_loader
                with replace_binding(
                    model_runner,
                    "get_model_loader",
                    lambda config: SharedTargetLoader(factory(config), self),
                ):
                    return super().load_model(*args, **kwargs)
            return super().load_model(*args, **kwargs)

    def set_external_speculation_io(self, io):
        """Bind one Worker-owned Connector before the role admits requests."""
        if self.external_speculation_io is not None:
            raise RuntimeError("external speculation IO is already installed")
        if type(self.sampler) is not _ProposalSampler:
            raise ValueError("DT requires the standard MRV2 sampler")
        self.external_speculation_io = io
        self.sampler.capture_proposals = io.capture_proposals and not self.context_role

    def add_requests(self, scheduler_output):
        """Bind Draft artifact metadata after MRV2 creates persistent requests."""
        super().add_requests(scheduler_output)
        if self.external_speculation_io is not None:
            for request in scheduler_output.scheduled_new_reqs:
                if self.context_role == "draft":
                    extra = request.sampling_params.extra_args
                    self.context_requests[request.req_id] = (
                        extra["dt_context_artifact"],
                        extra["dt_anchor_token"],
                        request.sampling_params.max_tokens,
                    )
                self.external_speculation_io.add_request(
                    request.req_id, request.sampling_params
                )

    def _remove_request(self, req_id):
        """Remove native slots; Target round resources outlive KV preemption."""
        if self.external_speculation_io is not None and not self.external_speculation:
            self.external_speculation_io.remove_request(req_id)
        self.context_requests.pop(req_id, None)
        return super()._remove_request(req_id)

    def finish_requests(self, scheduler_output):
        """Release Target round bindings only on completion/abort, not slot eviction."""
        if self.external_speculation:
            for req_id in scheduler_output.finished_req_ids:
                if self.external_speculation_io is not None:
                    self.external_speculation_io.remove_request(req_id)
                if self.context_role == "target":
                    self.speculator.requests.pop(req_id, None)
        return super().finish_requests(scheduler_output)

    def prepare_inputs(self, scheduler_output, *args, **kwargs):
        """Fill candidate slots before native input assembly and retain tickets."""
        if self.external_speculation:
            for req_id, tokens in scheduler_output.scheduled_spec_decode_tokens.items():
                slot = self.req_states.req_id_to_index[req_id]
                self.req_states.draft_tokens[slot, : len(tokens)] = torch.tensor(
                    tokens, dtype=self.req_states.draft_tokens.dtype, device=self.device
                )
        batch = super().prepare_inputs(scheduler_output, *args, **kwargs)
        if self.external_speculation:
            batch.external_draft_generations = (
                {} if self.warming_up else scheduler_output.external_draft_generations
            )
            batch.external_draft_recovery = (
                {} if self.warming_up else scheduler_output.external_draft_recovery
            )
        if self.context_role == "draft":
            self.context_batch = batch
        return batch

    def execute_model(self, *args, **kwargs):
        """Limit staged feature input ownership to one native execution call."""
        self.context_batch = None
        try:
            return super().execute_model(*args, **kwargs)
        finally:
            self.context_batch = None

    def sample_tokens(self, grammar_output):
        """Return a native neural proposal as the terminal output of a Draft request."""
        if (
            self.context_role != "draft"
            or self.execute_model_state is None
            or self.warming_up
        ):
            return super().sample_tokens(grammar_output)
        batch = self.execute_model_state.input_batch
        # Native postprocessing must consume the known anchor before propose().
        # Defer its output copy until the proposal exists; scheduling, model
        # execution, draft KV and request-state updates remain native.
        with replace_binding(model_runner, "AsyncOutput", lambda **kwargs: kwargs):
            output = super().sample_tokens(grammar_output)
        sampler = output["sampler_output"]
        tokens = self.req_states.draft_tokens[batch.idx_mapping].clone()
        self.external_speculation_io.on_neural_proposal(
            batch,
            tokens,
            self.speculator.draft_logits,
            self.sampler.sampling_states.temperature.gpu,
            [self.context_requests[req_id][2] for req_id in batch.req_ids],
        )
        sampler.sampled_token_ids = tokens
        sampler.num_sampled = sampler.num_sampled * self.num_speculative_steps
        output["num_sampled_tokens"] = sampler.num_sampled
        # No candidate IDs may feed back into native local speculative scheduling.
        return model_runner.AsyncOutput(**output)

    def sample(self, hidden_states, input_batch, grammar_output):
        """Supply remote q to native rejection sampling without a local drafter."""
        io = self.external_speculation_io
        if self.context_role == "draft" and not self.warming_up:
            from vllm.v1.worker.gpu.sample.output import SamplerOutput

            anchors = [
                self.context_requests[req_id][1] for req_id in input_batch.req_ids
            ]
            complete = (
                input_batch.num_computed_tokens_np + input_batch.num_scheduled_tokens
                >= input_batch.prefill_len_np
            )
            counts = torch.tensor(complete, dtype=torch.int32, device=self.device)
            output = SamplerOutput(
                sampled_token_ids=torch.tensor(
                    anchors, dtype=torch.int64, device=self.device
                )[:, None],
                logprobs_tensors=None,
                num_nans=None,
                num_sampled=counts,
                num_rejected=torch.zeros_like(counts),
            )
            return output, output.num_sampled, output.num_rejected
        if self.external_speculation and input_batch.num_draft_tokens:
            draft_logits = None
            if (
                self.speculative_config.draft_sample_method == "probabilistic"
                and not self.warming_up
            ):
                if io is None:
                    raise RuntimeError("external proposals have no device IO")
                draft_logits = io.draft_logits(
                    input_batch, self.sampler.sampling_states.temperature.gpu
                )
            # The native sampling method reads only speculator.draft_logits.
            # Restore the feature collector (or None) before native postprocessing.
            speculator = self.speculator
            self.speculator = SimpleNamespace(draft_logits=draft_logits)
            try:
                output = super().sample(hidden_states, input_batch, grammar_output)
            finally:
                self.speculator = speculator
        else:
            output = super().sample(hidden_states, input_batch, grammar_output)
        if self.external_speculation and not self.warming_up:
            for index, req_id in enumerate(input_batch.req_ids):
                recovery = input_batch.external_draft_recovery
                if req_id not in recovery:
                    continue
                # Replay reconstructs the same prefix, never a new confirmed token.
                # Native postprocessing uses num_rejected to roll the GPU KV cursor
                # back to the anchor; Scheduler applies the matching CPU rollback.
                output[1][index] = 0
                output[2][index] = int(recovery[req_id])
        if io is not None:
            io.on_sample(input_batch, output[0])
        return output

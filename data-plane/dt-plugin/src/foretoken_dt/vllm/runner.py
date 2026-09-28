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
    """Scope a factory substitution to synchronous Worker initialization."""
    original = getattr(module, name)
    setattr(module, name, replacement)
    try:
        yield
    finally:
        setattr(module, name, original)


class _ProposalSampler(Sampler):
    """Retain the native sampler's actual distribution for a Draft observer."""

    capture_proposals = False

    def _sample_random(
        self,
        processed_logits,
        expanded_idx_mapping,
        idx_mapping_np,
        pos,
        top_k,
        top_p,
        use_fused_sampler,
    ):
        """Retain native truncated logits when the Worker exports Draft q."""
        # Fused sampling does not materialize the truncated distribution. Keep
        # all processing and sampling in the native implementation, selecting
        # its existing materialized path only while exporting proposals.
        sampled, processed = super()._sample_random(
            processed_logits,
            expanded_idx_mapping,
            idx_mapping_np,
            pos,
            top_k,
            top_p,
            use_fused_sampler and not self.capture_proposals,
        )
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
        self.external_speculation = (
            speculative is not None and speculative.method == "external"
        )
        if self.external_speculation:
            # MRV2 has no external-speculator factory. Retain its speculative
            # buffers and rejection sampler, without constructing a local model.
            with replace_binding(model_runner, "init_speculator", lambda *_: None):
                super().__init__(vllm_config, device)
            self.draft_tokens_handler = _ExternalDraftTokens()
        else:
            super().__init__(vllm_config, device)

    def load_model(self, *args, **kwargs):
        """Construct the proposal-aware sampler through native model loading."""
        with replace_binding(model_runner, "Sampler", _ProposalSampler):
            return super().load_model(*args, **kwargs)

    def set_external_speculation_io(self, io):
        """Bind one Worker-owned Connector before the role admits requests."""
        if self.external_speculation_io is not None:
            raise RuntimeError("external speculation IO is already installed")
        if type(self.sampler) is not _ProposalSampler:
            raise ValueError("DT requires the standard MRV2 sampler")
        self.external_speculation_io = io
        self.sampler.capture_proposals = io.capture_proposals

    def add_requests(self, scheduler_output):
        """Bind Draft artifact metadata after MRV2 creates persistent requests."""
        super().add_requests(scheduler_output)
        if self.external_speculation_io is not None:
            for request in scheduler_output.scheduled_new_reqs:
                self.external_speculation_io.add_request(
                    request.req_id, request.sampling_params
                )

    def _remove_request(self, req_id):
        """Drop Connector bindings with the native request lifecycle."""
        if self.external_speculation_io is not None:
            self.external_speculation_io.remove_request(req_id)
        return super()._remove_request(req_id)

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
        return batch

    def sample(self, hidden_states, input_batch, grammar_output):
        """Supply remote q to native rejection sampling without a local drafter."""
        io = self.external_speculation_io
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
            # Restore None before native postprocessing can launch a drafter.
            self.speculator = SimpleNamespace(draft_logits=draft_logits)
            try:
                output = super().sample(hidden_states, input_batch, grammar_output)
            finally:
                self.speculator = None
        else:
            output = super().sample(hidden_states, input_batch, grammar_output)
        if io is not None:
            io.on_sample(input_batch, output[0])
        return output

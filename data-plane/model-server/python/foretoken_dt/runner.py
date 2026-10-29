# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Thin MRV2 Draft adapters for feature inputs and native proposal outputs."""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace

import torch
from vllm.v1.outputs import ModelRunnerOutput
from vllm.v1.worker.gpu import model_runner
from vllm.v1.worker.gpu.sample.output import SamplerOutput
from vllm.v1.worker.gpu.sample.sampler import Sampler

from .context import SharedTargetLoader
from .device_input import DeviceTurnFeedback
from .nccl import NcclPeer
from .protocol import OpenPeer
from .scheduler import ConfirmedDeviceTurn, DraftCachedRequests, ReservedDraftTurn


@dataclass
class DraftModelRunnerOutput(ModelRunnerOutput):
    """Return candidates and the independently confirmed Target input to its Scheduler."""

    confirmed_turns: dict[str, ConfirmedDeviceTurn] = field(default_factory=dict)


class DeviceTurnOutput(model_runner.AsyncOutput):
    """Retire device feedback through the native asynchronous output completion."""

    def __init__(self, feedback, contexts, **kwargs):
        self.feedback = feedback
        self.contexts = contexts
        super().__init__(**kwargs)

    def get_output(self):
        """Publish CPU confirmation only after native output DMA has completed."""
        output = super().get_output()
        output.confirmed_turns = self.feedback.settle(
            self.contexts, ConfirmedDeviceTurn
        )
        return output


@contextmanager
def replace_binding(module, name, replacement):
    """Scope a native factory override to one serialized Worker operation."""
    original = getattr(module, name)
    setattr(module, name, replacement)
    try:
        yield
    finally:
        setattr(module, name, original)


class ProposalSampler(Sampler):
    """Expose the actual native sampling distribution to the Draft output adapter."""

    capture_proposals = False

    def sample(self, *args, **kwargs):
        """Retain processed logits only while a real Draft turn consumes the distribution."""
        sampled, processed = super().sample(*args, **kwargs)
        if self.capture_proposals:
            self.proposal_logits = processed
        return sampled, processed


class DraftTokens:
    """Keep generated candidates out of the Draft engine's own speculative scheduling."""

    def set_draft_tokens(self, input_batch, draft_tokens):
        """The current turn returns these tokens as output, not as a future verification input."""

    def get_draft_tokens(self):
        """Return no speculative input to the Draft scheduler."""
        return


class DraftOutput:
    """Collect one peer's native GPU outputs in its original request order."""

    def __init__(
        self, peer: OpenPeer, channel: NcclPeer, requests, *, tokens, probabilities
    ):
        self.channel = channel
        self.tokens = tokens
        self.probabilities = probabilities
        self.pending = {
            session: (row, count, 0)
            for row, (session, _, _, _, count) in enumerate(requests)
            if count
        }

    def append(self, session, tokens, probabilities):
        """Publish the complete batch as soon as native sampling produces its last row."""
        torch.cuda.current_stream(self.tokens.device).wait_event(
            self.channel.input_ready
        )
        row, expected, offset = self.pending[session]
        count = min(tokens.numel(), expected - offset)
        self.tokens[row, offset : offset + count].copy_(tokens[:count])
        if self.probabilities is not None:
            if probabilities is None:
                raise RuntimeError(
                    "Native Draft output is missing its proposal probabilities"
                )
            self.probabilities[row, offset : offset + count].copy_(
                probabilities[:count]
            )
        offset += count
        if offset == expected:
            del self.pending[session]
        else:
            self.pending[session] = (row, expected, offset)
        if not self.pending:
            self.publish()

    def publish(self, producer_stream=None):
        """Queue completed rows or IO-initialized empty output before CPU dispatch."""
        self.channel.send_output(
            self.tokens,
            self.probabilities,
            producer_stream
            if producer_stream is not None
            else torch.cuda.current_stream(self.tokens.device),
        )


class DraftModelRunner(model_runner.GPUModelRunner):
    """Reuse native model, KV, sampling and graph execution for independently owned Draft turns."""

    def __init__(self, vllm_config, device):
        super().__init__(vllm_config, device)
        self.neural = vllm_config.speculative_config is not None
        self.warming_up = False
        self.context_features = None
        self.contexts = {}
        self.turn_inputs = {}
        self.probabilities = {}
        self.outputs: dict[str, DraftOutput] = {}
        self.device_inputs = {}
        self.device_feedback = None
        if self.neural:
            self.draft_tokens_handler = DraftTokens()

    def load_model(self, *args, **kwargs):
        """Keep native loading while selecting shared Target weights for neural proposals."""
        with replace_binding(model_runner, "Sampler", ProposalSampler):
            if not self.neural:
                return super().load_model(*args, **kwargs)
            factory = model_runner.get_model_loader
            with replace_binding(
                model_runner,
                "get_model_loader",
                lambda config: SharedTargetLoader(factory(config), self),
            ):
                return super().load_model(*args, **kwargs)

    def add_requests(self, scheduler_output):
        """Bind proposal metadata after native persistent slots are allocated or rebound."""
        super().add_requests(scheduler_output)
        for request in scheduler_output.scheduled_new_reqs:
            # Model-specific state consumes the confirmed prefix locally. Own
            # this list even when the native executor runs in the same process.
            self.turn_inputs[request.req_id] = replace(
                request, prompt_token_ids=list(request.prompt_token_ids)
            )

    def update_requests(self, scheduler_output):
        """Apply remote confirmations in existing slots after native KV block updates."""
        super().update_requests(scheduler_output)
        cached = scheduler_output.scheduled_cached_reqs
        if not isinstance(cached, DraftCachedRequests) or not cached.turns:
            return
        states = self.req_states
        for request_id, turn in cached.turns.items():
            slot = states.req_id_to_index[request_id]
            computed = int(states.num_computed_tokens_np[slot])
            states.all_token_ids.stage_write(slot, turn.token_offset, turn.token_ids)
            states.prompt_len.np[slot] = turn.prompt_length
            states.prefill_len.np[slot] = turn.prompt_length
            states.total_len.stage_write_elem(slot, turn.prompt_length)
            states.num_computed_prefill_tokens[slot] = computed
            states.num_computed_tokens.stage_write_elem(slot, computed)
            states.max_seq_len[slot] = (
                turn.prompt_length + turn.sampling_params.max_tokens
            )
            states.draft_tokens[slot].zero_()
            request = self.turn_inputs[request_id]
            request.prompt_token_ids[turn.token_offset :] = turn.token_ids
            request.prefill_token_ids = request.prompt_token_ids
            request.num_computed_tokens = computed
            request.sampling_params = turn.sampling_params
            self.model_state.add_request(slot, request)
            # Neural proposals consume only the session's fixed temperature and
            # seed. Ordinary sampling also owns processors that must see the
            # corrected prefix rather than its discarded candidates.
            if not self.neural:
                self.sampler.add_request(slot, turn.sampling_params)
        states.apply_staged_writes()
        self.model_state.apply_staged_writes()
        if not self.neural:
            self.sampler.apply_staged_writes()

    def _remove_request(self, req_id):
        self.turn_inputs.pop(req_id, None)
        return super()._remove_request(req_id)

    def prepare_inputs(self, scheduler_output, *args, **kwargs):
        """Stage only the scheduled confirmed features into stable native graph inputs."""
        batch = super().prepare_inputs(scheduler_output, *args, **kwargs)
        cached = scheduler_output.scheduled_cached_reqs
        reservations = (
            dict(cached.reserved_turns)
            if isinstance(cached, DraftCachedRequests)
            else {}
        )
        for request in scheduler_output.scheduled_new_reqs:
            plan = (request.sampling_params.extra_args or {}).get("dt_reserved_turn")
            if plan is not None:
                reservations[request.req_id] = ReservedDraftTurn(**plan)
        if reservations:
            return self._prepare_device_inputs(batch, reservations)
        if self.neural and not self.warming_up:
            for row, request_id in enumerate(batch.req_ids):
                session = self.turn_inputs[request_id].sampling_params.extra_args[
                    "dt_session"
                ]
                start = int(batch.num_computed_tokens_np[row])
                left, right = batch.query_start_loc_np[row : row + 2]
                self.context_features[left:right].copy_(
                    self.contexts[session].read(start, int(right - left)),
                    non_blocking=True,
                )
            self.context_features[
                batch.num_tokens : batch.num_tokens_after_padding
            ].zero_()
        return batch

    def _prepare_device_inputs(self, batch, reservations):
        """Bind logical Target tensors to native Draft slots and physical block tables."""
        if set(batch.req_ids) != set(reservations):
            raise RuntimeError(
                "Reserved device turns must occupy one complete native batch"
            )
        rows = []
        upper_bounds = []
        anchors = []
        for row, request_id in enumerate(batch.req_ids):
            binding = self.device_inputs[request_id]
            reservation = reservations[request_id]
            if (
                reservation.turn_sequence != binding.batch.sequence
                or reservation.query_tokens != binding.count
                or reservation.initial != binding.initial
            ):
                raise RuntimeError("Native reservation differs from its device input")
            self.main_stream.wait_event(binding.batch.ready)
            left, right = batch.query_start_loc_np[row : row + 2]
            source = slice(binding.offset, binding.offset + binding.count)
            batch.input_ids[left:right].copy_(binding.batch.input_ids[source])
            batch.positions[left:right].copy_(binding.batch.positions[source])
            batch.seq_lens[row].copy_(binding.batch.seq_lens[binding.row])
            self.context_features[left:right].copy_(
                binding.batch.features[:, source].movedim(0, 1)
            )
            anchors.append(binding.batch.last_sampled[binding.row])
            upper_bounds.append(binding.seq_len_upper_bound)
            rows.append((request_id, binding))
        self.req_states.last_sampled_tokens[batch.idx_mapping] = torch.stack(anchors)[
            :, None
        ]
        self.context_features[batch.num_tokens : batch.num_tokens_after_padding].zero_()
        self.device_feedback = DeviceTurnFeedback(self, rows)
        return replace(
            batch,
            seq_lens_cpu_upper_bound=torch.tensor(upper_bounds, dtype=torch.int32),
        )

    def sample(self, hidden_states, input_batch, grammar_output):
        """Supply the confirmed anchor for neural Drafts; ordinary Drafts sample natively."""
        if self.warming_up:
            return super().sample(hidden_states, input_batch, grammar_output)
        complete = (
            input_batch.num_computed_tokens_np + input_batch.num_scheduled_tokens
            >= input_batch.prefill_len_np
        )
        if self.neural:
            anchors = [
                self.turn_inputs[request_id].sampling_params.extra_args[
                    "dt_anchor_token"
                ]
                for request_id in input_batch.req_ids
            ]
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
        with replace_binding(self.sampler, "capture_proposals", True):
            output = super().sample(hidden_states, input_batch, grammar_output)
        # All DT requests have an explicit seed. Native sampling therefore
        # materializes the same processed logits used by the actual draw.
        for row, request_id in enumerate(input_batch.req_ids):
            if not complete[row]:
                continue
            extra = self.turn_inputs[request_id].sampling_params.extra_args
            session = extra["dt_session"]
            destination = self.outputs.get(session)
            log_q = (
                self.sampler.proposal_logits[row].float().log_softmax(dim=-1)
                if extra["dt_capture_q"]
                else None
            )
            if destination is not None:
                if session in destination.pending:
                    destination.append(
                        session,
                        output[0].sampled_token_ids[row].reshape(-1),
                        log_q[None] if log_q is not None else None,
                    )
            elif log_q is not None and not extra.get("dt_gpu_output", False):
                self.probabilities[session].append(log_q)
        self.sampler.proposal_logits = None
        return output

    def sample_tokens(self, grammar_output):
        """Publish native propose() candidates as this retained request's bounded output turn."""
        if self.device_feedback is not None:
            return self._sample_device_turn()
        if not self.neural or self.warming_up or self.execute_model_state is None:
            return super().sample_tokens(grammar_output)
        batch = self.execute_model_state.input_batch
        # Postprocessing first consumes the confirmed anchor and runs the native
        # proposer. Defer D2H until candidates, rather than that anchor, exist.
        with replace_binding(model_runner, "AsyncOutput", lambda **kwargs: kwargs):
            output = super().sample_tokens(grammar_output)
        sampler = output["sampler_output"]
        tokens = self.req_states.draft_tokens[batch.idx_mapping]
        complete = (
            batch.num_computed_tokens_np + batch.num_scheduled_tokens
            >= batch.prefill_len_np
        )
        for row, request_id in enumerate(batch.req_ids):
            if not complete[row]:
                continue
            params = self.turn_inputs[request_id].sampling_params
            extra = params.extra_args
            session = extra["dt_session"]
            log_q = None
            if extra["dt_capture_q"]:
                slot = int(batch.idx_mapping_np[row])
                logits = self.speculator.draft_logits[slot, : params.max_tokens].float()
                temperature = self.sampler.sampling_states.temperature.gpu[
                    slot
                ].clamp_min(1e-20)
                log_q = (logits / temperature).log_softmax(dim=-1)
            destination = self.outputs.get(session)
            if destination is not None:
                if session in destination.pending:
                    destination.append(session, tokens[row, : params.max_tokens], log_q)
            elif log_q is not None and not extra.get("dt_gpu_output", False):
                self.probabilities[session] = list(log_q.unbind(dim=0))
        sampler.sampled_token_ids = tokens
        sampler.num_sampled = sampler.num_sampled * self.num_speculative_steps
        output["num_sampled_tokens"] = sampler.num_sampled
        return model_runner.AsyncOutput(**output)

    def _sample_device_turn(self):
        """Execute pre-scheduled neural proposals directly from Target device confirmation."""
        state, self.execute_model_state = self.execute_model_state, None
        feedback, self.device_feedback = self.device_feedback, None
        batch = state.input_batch
        sampled = torch.stack(
            [binding.batch.num_sampled[binding.row] for _, binding in feedback.rows]
        ).to(torch.int32)
        rejected = torch.stack(
            [binding.batch.num_rejected[binding.row] for _, binding in feedback.rows]
        ).to(torch.int32)
        features = self.context_features[: batch.num_tokens_after_padding]
        aux = (
            list(features.unbind(dim=1)) if self.use_aux_hidden_state_outputs else None
        )
        tokens = self.speculator.propose(
            batch,
            state.attn_metadata,
            state.slot_mappings_by_layer,
            features[:, 0],
            aux,
            sampled,
            rejected,
            self.req_states.last_sampled_tokens,
            self.req_states.next_prefill_tokens,
            self.sampler.sampling_states.temperature.gpu,
            self.sampler.sampling_states.seeds.gpu,
            dp_sync=state.dp_sync,
        ).clone()
        for row, request_id in enumerate(batch.req_ids):
            destination = self.outputs[request_id]
            log_q = None
            if destination.probabilities is not None:
                slot = int(batch.idx_mapping_np[row])
                logits = self.speculator.draft_logits[slot].float()
                temperature = self.sampler.sampling_states.temperature.gpu[
                    slot
                ].clamp_min(1e-20)
                log_q = (logits / temperature).log_softmax(dim=-1)
            destination.append(request_id, tokens[row], log_q)
        counts = torch.full(
            (batch.num_reqs,), tokens.shape[1], dtype=torch.int32, device=self.device
        )
        sampler_output = SamplerOutput(
            sampled_token_ids=tokens,
            logprobs_tensors=None,
            num_nans=None,
            num_sampled=counts,
            num_rejected=torch.zeros_like(counts),
        )
        output = DraftModelRunnerOutput(
            req_ids=batch.req_ids,
            req_id_to_index={
                request_id: row for row, request_id in enumerate(batch.req_ids)
            },
            cudagraph_stats=state.cudagraph_stats,
        )
        return DeviceTurnOutput(
            feedback=feedback,
            contexts=self.contexts,
            model_runner_output=output,
            sampler_output=sampler_output,
            num_sampled_tokens=counts,
            main_stream=self.main_stream,
            copy_stream=self.output_copy_stream,
            check_ep_fault=self.check_ep_fault,
            pending_aux_output=None,
        )

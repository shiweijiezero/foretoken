# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retained draft turns layered on native synchronous and asynchronous scheduling."""

import time
from copy import copy
from dataclasses import dataclass, field
from functools import partial

from vllm import SamplingParams
from vllm.v1.core.sched.async_scheduler import AsyncScheduler
from vllm.v1.core.sched.interface import PauseState
from vllm.v1.core.sched.output import CachedRequestData
from vllm.v1.core.sched.scheduler import Scheduler
from vllm.v1.request import RequestStatus


@dataclass
class ConfirmedDraftTurn:
    """Carry a confirmed suffix and turn parameters to an already allocated Worker slot."""

    token_offset: int
    token_ids: list[int]
    prompt_length: int
    sampling_params: SamplingParams


@dataclass
class DraftCachedRequests(CachedRequestData):
    """Extend native cached scheduling with remote confirmation updates, not KV ownership."""

    turns: dict[str, ConfirmedDraftTurn] = field(default_factory=dict)


class DraftTurns:
    """Keep one streaming request and private native KV for each remote draft session."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dt_waiting_queues = (self.waiting, self.kv_holding_waiting)
        if self.cache_config.enable_prefix_caching:
            coordinator = self.kv_cache_manager.coordinator
            cache_blocks = coordinator.cache_blocks

            def cache_confirmed_blocks(request, num_computed_tokens):
                """Publish only Target-confirmed KV through the native cache coordinator."""
                # Native AR output is still speculative here. Publishing its
                # blocks would expose mutable, rejectable KV to other requests.
                cache_blocks(
                    request, min(num_computed_tokens, request.num_prompt_tokens)
                )

            coordinator.cache_blocks = cache_confirmed_blocks

            def lookup_confirmed_blocks(lookup, request):
                """Keep native cache hits within immutable input when replaying a preempted turn."""
                if request.num_tokens > request.num_prompt_tokens:
                    request = copy(request)
                    # Native lookup excludes the final token. One output token
                    # lets it reuse the full confirmed prefix, never its guesses.
                    request._all_token_ids = request._all_token_ids[
                        : request.num_prompt_tokens + 1
                    ]
                return lookup(request)

            manager = self.kv_cache_manager
            manager.get_computed_blocks = partial(
                lookup_confirmed_blocks, manager.get_computed_blocks
            )
            manager.get_computed_blocks_for_connector = partial(
                lookup_confirmed_blocks, manager.get_computed_blocks_for_connector
            )
        if self.num_spec_tokens:
            # Feature-conditioned requests cannot fill synthetic prompt positions.
            self.dynamic_sd_lookup = [self.num_spec_tokens] * (
                self.scheduler_config.max_num_seqs + 1
            )

    def add_request(self, request):
        """Admit a received Target turn together, then leave execution batching to native scheduling."""
        previous = self.requests.get(request.request_id)
        if not request.resumable:
            self.finish_requests(request.request_id, RequestStatus.FINISHED_ABORTED)
            return
        if previous is not None:
            if not previous.dt_waiting:
                raise ValueError(
                    "Draft input arrived before the previous turn finished"
                )
            for queue in self.dt_waiting_queues:
                queue.remove_requests((previous,))
            if previous.status != RequestStatus.WAITING_FOR_STREAMING_REQ:
                previous.status = RequestStatus.WAITING_FOR_STREAMING_REQ
                self.num_waiting_for_streaming_input += 1
        else:
            request.dt_waiting = False
            request.dt_turn = None
        (previous or request).dt_batch_pending = True
        super().add_request(request)
        current = self.requests[request.request_id]
        current.dt_waiting = False
        if current in self.running:
            current.status = RequestStatus.RUNNING
        else:
            if previous is not None:
                self._enqueue_waiting_request(current)
            current.status = RequestStatus.WAITING_FOR_STREAMING_REQ
            self.num_waiting_for_streaming_input += 1

        # The native client sends individual ADD messages. Keep its existing
        # requests ineligible until the whole received turn has reached Core;
        # otherwise the first arrivals form avoidable one-request GPU batches.
        extra = current.sampling_params.extra_args
        members = [
            item
            for item in self.requests.values()
            if item.sampling_params.extra_args["dt_batch"] == extra["dt_batch"]
        ]
        if len(members) != extra["dt_batch_size"]:
            return
        for member in members:
            member.dt_batch_pending = False
            member.next_decode_eligible_step = 0
            if member.status == RequestStatus.WAITING_FOR_STREAMING_REQ:
                self.num_waiting_for_streaming_input -= 1
                member.status = RequestStatus.WAITING

    def _make_cached_request_data(self, running_reqs, *args, **kwargs):
        """Publish confirmed suffixes alongside the native incremental block updates."""
        cached = super()._make_cached_request_data(running_reqs, *args, **kwargs)
        return DraftCachedRequests(
            req_ids=cached.req_ids,
            resumed_req_ids=cached.resumed_req_ids,
            new_token_ids=cached.new_token_ids,
            all_token_ids=cached.all_token_ids,
            new_block_ids=cached.new_block_ids,
            num_computed_tokens=cached.num_computed_tokens,
            num_output_tokens=cached.num_output_tokens,
            turns={
                request.request_id: request.dt_turn
                for request in running_reqs
                if request.dt_turn is not None
            },
        )

    def _update_after_schedule(self, output):
        """Consume scheduled confirmations and account for whole neural proposals."""
        for request_id in output.num_scheduled_tokens:
            self.requests[request_id].dt_turn = None
        super()._update_after_schedule(output)
        if self.num_spec_tokens:
            for request_id in output.num_scheduled_tokens:
                request = self.requests[request_id]
                if request.is_prefill_chunk:
                    continue
                if isinstance(self, AsyncScheduler):
                    # Native async accounting reserves an AR output. A completed
                    # neural turn publishes its requested proposal prefix instead.
                    native_count = self.num_sampled_tokens_per_step + len(
                        output.scheduled_spec_decode_tokens.get(request_id, ())
                    )
                    request.num_output_placeholders += request.max_tokens - native_count
                request.spec_token_ids = []

    def _update_request_as_session(self, session, update):
        """Discard speculative tails before replacing a turn with its confirmed prefix."""
        delta = update.prompt_token_ids
        extra = update.sampling_params.extra_args
        previous_extra = session.sampling_params.extra_args
        if extra["dt_generation"] <= previous_extra["dt_generation"]:
            raise ValueError("Draft turn did not advance its confirmed prefix")
        base = len(session.prompt_token_ids)
        prompt_length = base + len(delta)
        reused = base
        if self.num_spec_tokens:
            if not delta or delta[0] != previous_extra["dt_anchor_token"]:
                raise ValueError("Draft confirmation changed its previous anchor")
        else:
            for old, new in zip(session.all_token_ids[base:], delta):
                if old != new:
                    break
                reused += 1
            reused = min(reused, prompt_length - 1)
        reused = min(reused, session.num_computed_tokens)
        session.dt_turn = ConfirmedDraftTurn(
            base, delta, prompt_length, update.sampling_params
        )

        # Native session resumption retains computed output tokens and appends
        # new input. Limit that frontier to the prefix confirmed by the Target.
        update.prompt_token_ids = (
            session.prompt_token_ids[reused:] + delta
            if reused < base
            else delta[reused - base :]
        )
        session.num_computed_tokens = reused
        del session._all_token_ids[reused:]
        session._output_token_ids.clear()
        del session.prompt_token_ids[min(reused, base) :]
        session.num_prompt_tokens = min(reused, base)
        session.spec_token_ids = []
        session.block_hashes.clear()
        super()._update_request_as_session(session, update)
        session.next_decode_eligible_step = 0

    def _handle_stopped_request(self, request):
        """Park a completed turn at the native materialized frontier, retaining reusable KV."""
        request.num_computed_tokens -= request.num_output_placeholders
        request.spec_token_ids = []
        request.drop_stale_output = True
        request.num_stale_output_tokens = request.num_in_flight_tokens
        request.num_output_placeholders = 0
        request.dt_waiting = True
        request.status = (
            RequestStatus.RUNNING
            if request.num_computed_tokens
            else RequestStatus.PREEMPTED
        )
        return False

    def update_from_output(self, scheduler_output, model_runner_output):
        """Retain parked turns after native stop handling removes them from the running list."""
        outputs = super().update_from_output(scheduler_output, model_runner_output)
        for request_id in scheduler_output.num_scheduled_tokens:
            request = self.requests.get(request_id)
            if (
                request is not None
                and request.dt_waiting
                and request.status == RequestStatus.RUNNING
                and request not in self.running
            ):
                self.running.append(request)
        return outputs

    def _preempt_request(self, request, timestamp, drop_stale_output=False):
        """Let native pressure reclaim private KV without regenerating an idle proposal."""
        super()._preempt_request(request, timestamp, drop_stale_output)
        if request.dt_waiting or request.dt_batch_pending:
            request.status = RequestStatus.WAITING_FOR_STREAMING_REQ
            self.num_waiting_for_streaming_input += 1

    def _ready(self, request):
        return (
            not request.dt_waiting
            and not request.dt_batch_pending
            and (not self.num_spec_tokens or request.num_in_flight_tokens == 0)
        )

    def schedule(self, throttle_prefills=False):
        """Batch ready turns while retaining ordinary native allocation and preemption."""
        for request in self.running:
            if not self._ready(request):
                request.next_decode_eligible_step = self.current_step + 2
        try:
            output = super().schedule(throttle_prefills)
            queued = self.get_num_unfinished_requests() - len(self.running)
            if not output.total_num_scheduled_tokens and queued > 0:
                # Retained turns can occupy slots or KV needed by another
                # Target. Reclaim enough idle slots for native batch admission;
                # freeing only one would serialize a queued Target batch.
                available = self.max_num_active_reqs - len(self.running)
                to_reclaim = max(1, min(queued, self.max_num_active_reqs) - available)
                for request in reversed(self.running[:]):
                    if request.dt_waiting and self._request_blocks_can_be_freed(
                        request
                    ):
                        self.running.remove(request)
                        self._preempt_request(request, time.monotonic())
                        to_reclaim -= 1
                        if not to_reclaim:
                            break
            return output
        finally:
            for request in self.requests.values():
                if request.dt_waiting and request not in self.running:
                    for queue in self.dt_waiting_queues:
                        queue.remove_requests((request,))
                    if request.status == RequestStatus.WAITING_FOR_STREAMING_REQ:
                        self.num_waiting_for_streaming_input -= 1
                    request.status = RequestStatus.PREEMPTED

    def has_requests(self):
        """Sleep between turns; native input, abort and in-flight batch results wake the engine."""
        if self.pause_state == PauseState.PAUSED_ALL:
            return self.has_finished_requests()
        return bool(
            self.has_finished_requests()
            or any(self._ready(request) for request in self.running)
            or (
                self.pause_state == PauseState.UNPAUSED
                and self.get_num_unfinished_requests() > len(self.running)
                and (
                    len(self.running) < self.scheduler_config.max_num_seqs
                    or any(request.dt_waiting for request in self.running)
                )
            )
        )


class DraftScheduler(DraftTurns, Scheduler):
    """Use native synchronous scheduling for retained Draft turns."""


class AsyncDraftScheduler(DraftTurns, AsyncScheduler):
    """Use native asynchronous batching without predicting a second unconfirmed turn."""

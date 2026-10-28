# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retained draft turns layered on native synchronous and asynchronous scheduling."""

import time
from contextlib import contextmanager
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
class ReservedDraftTurn:
    """Describe external device inputs whose KV capacity is already owned by native scheduling."""

    turn_sequence: int
    query_tokens: int
    seq_len_upper_bound: int
    feature_offset: int
    peer_id: str
    initial: bool = False


@dataclass
class ConfirmedDeviceTurn:
    """Return the actual Target-confirmed prefix independently of proposed Draft output."""

    turn_sequence: int
    token_ids_delta: list[int]
    confirmed_length: int
    context_length: int
    anchor: int
    initial: bool = False


@dataclass
class DraftCachedRequests(CachedRequestData):
    """Extend native cached scheduling with remote confirmation updates, not KV ownership."""

    turns: dict[str, ConfirmedDraftTurn] = field(default_factory=dict)
    reserved_turns: dict[str, ReservedDraftTurn] = field(default_factory=dict)


class DraftTurns:
    """Keep one streaming request and private native KV for each remote draft session."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # External inputs may still write private KV after cancellation. Use
        # native step fences before returning those blocks to its allocator.
        self.defer_block_free = True
        self._device_batch_active = False
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
        reserved = (request.sampling_params.extra_args or {}).get("dt_reserved_turn")
        if reserved is not None:
            if previous is None and request.dt_reserved_turn.initial:
                request.dt_waiting = False
                request.dt_turn = None
                super().add_request(request)
                return
            if previous is None or getattr(
                previous, "dt_reserved_turn", None
            ) != ReservedDraftTurn(**reserved):
                raise ValueError("Device turn has no matching native reservation")
            if not previous.dt_waiting or previous.num_in_flight_tokens:
                raise ValueError("Device turn arrived before native completion")
            # Native allocation must not treat unverified private Draft KV as
            # committed input; the worker retains it and consumes GPU lengths.
            previous.num_computed_tokens = min(
                previous.num_computed_tokens, previous.num_prompt_tokens
            )
            previous.dt_waiting = False
            previous.next_decode_eligible_step = 0
            if previous not in self.running:
                raise ValueError("Reserved Draft request lost its native worker slot")
            previous.status = RequestStatus.RUNNING
            return
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
            request.dt_reserved_turn = None
        super().add_request(request)
        current = self.requests[request.request_id]
        current.dt_waiting = False
        current.next_decode_eligible_step = 0
        if current in self.running:
            current.status = RequestStatus.RUNNING
        else:
            if previous is not None:
                self._enqueue_waiting_request(current)
            current.status = RequestStatus.WAITING

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
            reserved_turns={
                request.request_id: request.dt_reserved_turn
                for request in running_reqs
                if getattr(request, "dt_reserved_turn", None) is not None
            },
        )

    @contextmanager
    def prepare_batch_admission(self, requests, plans):
        """Prepare a complete external batch while native admission keeps request and slot ownership."""
        indexed = {request.request_id: request for request, _ in requests}
        try:
            ids = [plan["request_id"] for plan in plans]
        except (KeyError, TypeError) as error:
            raise ValueError(
                "External admission plan requires request identities"
            ) from error
        if (
            not plans
            or len(indexed) != len(requests)
            or len(ids) != len(set(ids))
            or set(indexed) != set(ids)
        ):
            raise ValueError(
                "External admission plan does not match its complete request batch"
            )
        prepared = []
        for plan in plans:
            incoming = indexed[plan["request_id"]]
            request = self.requests.get(incoming.request_id, incoming)
            try:
                turn = ReservedDraftTurn(
                    **{key: value for key, value in plan.items() if key != "request_id"}
                )
            except TypeError as error:
                raise ValueError(
                    "External admission plan has invalid reservation fields"
                ) from error
            supplied = (incoming.sampling_params.extra_args or {}).get(
                "dt_reserved_turn"
            )
            try:
                matches = supplied is not None and ReservedDraftTurn(**supplied) == turn
            except TypeError as error:
                raise ValueError(
                    "External admission request has invalid reservation fields"
                ) from error
            if not matches or not incoming.resumable:
                raise ValueError(
                    "External admission request has inconsistent reservation parameters"
                )
            if turn.initial:
                if (
                    request is not incoming
                    or not incoming.prompt_token_ids
                    or turn.query_tokens != incoming.num_prompt_tokens
                    or turn.seq_len_upper_bound != incoming.num_prompt_tokens
                    or incoming.num_computed_tokens
                ):
                    raise ValueError(
                        "Initial device turn must contain its complete new prompt"
                    )
            elif turn.query_tokens != self.num_spec_tokens + 1:
                raise ValueError(
                    "Decode device turn must preserve its complete padded query"
                )
            prepared.append((request, turn))
        if (
            not self.num_spec_tokens
            or self.pause_state != PauseState.UNPAUSED
            or self.need_mamba_block_aligned_split
            or self.num_prefill_lookahead > 1
            or self.connector is not None
            or self.ec_connector is not None
            or self.prefix_replay_tokens
            or self.cache_config.enable_prefix_caching
            or any(
                getattr(request, "dt_reserved_turn", None) is not None
                or request.num_in_flight_tokens
                or not request.dt_waiting
                for request in self.requests.values()
            )
        ):
            yield False
            return
        query_budget = sum(turn.query_tokens for _, turn in prepared)
        slots = self.vllm_config.speculative_config.max_num_new_slots_for_drafting
        new_count = sum(turn.initial for _, turn in prepared)
        if (
            query_budget > self.max_num_scheduled_tokens
            or query_budget + len(plans) * slots
            > self.scheduler_config.max_num_batched_tokens
            or len(self.running) + self.num_waiting_for_streaming_input + new_count
            > self.max_num_active_reqs
            or len(self.running) + new_count > self.max_num_running_reqs
        ):
            yield False
            return
        for request, turn in prepared:
            if (
                request.has_encoder_inputs
                or request.mm_features
                or request.lora_request is not None
                or 0
                < self.scheduler_config.long_prefill_token_threshold
                < turn.query_tokens
                or turn.seq_len_upper_bound + self.num_spec_tokens >= self.max_model_len
                or (
                    not turn.initial
                    and (
                        request not in self.running
                        or not request.dt_waiting
                        or request.num_computed_tokens
                        + turn.query_tokens
                        + self.num_sampled_tokens_per_step
                        > self.max_model_len
                    )
                )
            ):
                yield False
                return
        initial_requests = [request for request, turn in prepared if turn.initial]
        aborted = set()
        accepted = False
        try:
            # Running requests can grow first. Growth remains native-owned even
            # if a later row rejects preparation and the ordinary path resumes.
            ordered = sorted(prepared, key=lambda item: item[1].initial)
            for request, turn in ordered:
                materialized = request.num_computed_tokens
                request.num_computed_tokens = min(
                    materialized, request.num_prompt_tokens
                )
                try:
                    blocks = self.kv_cache_manager.allocate_slots(
                        request,
                        turn.query_tokens
                        if turn.initial
                        else max(
                            1, turn.seq_len_upper_bound - request.num_computed_tokens
                        ),
                        num_lookahead_tokens=self.num_lookahead_tokens,
                        delay_cache_blocks=True,
                        full_sequence_must_fit=turn.initial
                        and self.scheduler_reserve_full_isl,
                        has_scheduled_reqs=True,
                    )
                finally:
                    request.num_computed_tokens = materialized
                if blocks is None:
                    yield False
                    return
            # Native waiting admission checks the watermark again even when
            # preallocation makes its additional block demand zero.
            if (
                initial_requests
                and self.kv_cache_manager.block_pool.get_num_free_blocks()
                < self.kv_cache_manager.watermark_blocks
            ):
                yield False
                return
            for request, turn in prepared:
                request.dt_reserved_turn = turn
                if turn.initial:
                    request.dt_waiting = False
                    request.dt_turn = None
            accepted = True
            yield True
        except BaseException:
            admitted = [
                request.request_id
                for request, _ in prepared
                if request.request_id in self.requests
                and not self.requests[request.request_id].dt_waiting
            ]
            if admitted:
                aborted.update(admitted)
                self.finish_requests(admitted, RequestStatus.FINISHED_ABORTED)
            for request, _ in prepared:
                if (
                    request.request_id in self.requests
                    and self.requests[request.request_id].dt_waiting
                ):
                    request.dt_reserved_turn = None
            raise
        finally:
            if not accepted:
                for request, _ in prepared:
                    if request.request_id not in self.requests:
                        request.dt_reserved_turn = None
            for request in initial_requests:
                if (
                    request.request_id not in self.requests
                    and request.request_id not in aborted
                ):
                    self.kv_cache_manager.free(request)

    def dt_request_states(self, rows):
        """Expose settled resumable prefixes to the peer owner after native turn completion."""
        states = []
        for request_id, previous in rows:
            request = self.requests[request_id]
            if not request.dt_waiting or request.num_in_flight_tokens:
                raise ValueError("Draft request still has native work in flight")
            extra = request.sampling_params.extra_args
            prefix = request.prompt_token_ids + [extra["dt_anchor_token"]]
            states.append(
                {
                    "request_id": request_id,
                    "confirmed_length": len(prefix),
                    "context_length": len(prefix) - 1,
                    "token_ids_delta": prefix[previous:],
                    "anchor": prefix[-1],
                }
            )
        return states

    def get_num_new_tokens(self, request):
        """Schedule a reserved input shape without appending padding to the confirmed prefix."""
        turn = getattr(request, "dt_reserved_turn", None)
        if turn is not None:
            return turn.query_tokens
        return super().get_num_new_tokens(request)

    def _request_blocks_can_be_freed(self, request):
        if (
            not request.is_finished()
            and getattr(request, "dt_reserved_turn", None) is not None
        ):
            return False
        return super()._request_blocks_can_be_freed(request)

    def _handle_blocked_waiting_request(self, request):
        if (
            self._device_batch_active
            and getattr(request, "dt_reserved_turn", None) is None
        ):
            return False
        return super()._handle_blocked_waiting_request(request)

    def _update_after_schedule(self, output):
        """Consume scheduled confirmations and account for whole neural proposals."""
        # A failed reservation can leave native growth attached to a parked request.
        # Its next external turn must publish the complete table, not only this step's delta.
        for request_id in output.scheduled_cached_reqs.req_ids:
            request = self.requests[request_id]
            if getattr(request, "dt_reserved_turn", None) is not None or (
                self.num_spec_tokens and request.dt_turn is not None
            ):
                if output.block_table_updates is None:
                    output.block_table_updates = {}
                output.block_table_updates[request_id] = (
                    self.kv_cache_manager.get_block_ids(request_id)
                )
        for request_id in output.num_scheduled_tokens:
            self.requests[request_id].dt_turn = None
        super()._update_after_schedule(output)
        if self.num_spec_tokens:
            for request_id in output.num_scheduled_tokens:
                request = self.requests[request_id]
                if getattr(request, "dt_reserved_turn", None) is not None:
                    request.num_output_placeholders = 0
                    request.spec_token_ids = []
                    continue
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
        pending = session.dt_turn
        session.dt_turn = ConfirmedDraftTurn(
            pending.token_offset if pending is not None else base,
            pending.token_ids + delta if pending is not None else delta,
            prompt_length,
            update.sampling_params,
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
        if getattr(request, "dt_reserved_turn", None) is None:
            request.num_computed_tokens -= request.num_output_placeholders
        request.spec_token_ids = []
        request.drop_stale_output = True
        request.num_stale_output_tokens = request.num_in_flight_tokens
        request.num_output_placeholders = 0
        request.dt_waiting = True
        request.dt_reserved_turn = None
        request.status = (
            RequestStatus.RUNNING
            if request.num_computed_tokens
            else RequestStatus.PREEMPTED
        )
        return False

    def update_from_output(self, scheduler_output, model_runner_output):
        """Retain parked turns after native stop handling removes them from the running list."""
        for request_id, confirmed in getattr(
            model_runner_output, "confirmed_turns", {}
        ).items():
            request = self.requests.get(request_id)
            if request is None or request.is_finished():
                continue
            turn = request.dt_reserved_turn
            if turn is None or turn.turn_sequence != confirmed.turn_sequence:
                raise ValueError(
                    "Device confirmation does not match the scheduled turn"
                )
            delta = confirmed.token_ids_delta
            extra = request.sampling_params.extra_args
            if confirmed.initial:
                if (
                    not turn.initial
                    or delta != [confirmed.anchor]
                    or confirmed.confirmed_length != request.num_prompt_tokens + 1
                    or confirmed.context_length != request.num_prompt_tokens
                ):
                    raise ValueError(
                        "Initial device confirmation does not match its complete prompt"
                    )
                prompt_delta = []
            else:
                if (
                    turn.initial
                    or not delta
                    or confirmed.confirmed_length
                    != request.num_prompt_tokens + 1 + len(delta)
                ):
                    raise ValueError(
                        "Device confirmation does not extend the retained prefix"
                    )
                prompt_delta = [extra["dt_anchor_token"], *delta[:-1]]
            if (
                confirmed.anchor != delta[-1]
                or confirmed.context_length + 1 != confirmed.confirmed_length
            ):
                raise ValueError("Device confirmation has an inconsistent anchor")
            offset = request.num_prompt_tokens
            request.prompt_token_ids.extend(prompt_delta)
            request._all_token_ids = request.prompt_token_ids.copy()
            request._output_token_ids.clear()
            request.num_prompt_tokens = len(request.prompt_token_ids)
            request.num_computed_tokens = confirmed.context_length
            request.num_output_placeholders = 0
            request.spec_token_ids = []
            request.block_hashes.clear()
            extra["dt_anchor_token"] = confirmed.anchor
            extra["dt_generation"] = confirmed.confirmed_length
            request.dt_turn = (
                ConfirmedDraftTurn(
                    offset,
                    prompt_delta,
                    request.num_prompt_tokens,
                    request.sampling_params,
                )
                if prompt_delta
                else None
            )
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

    def _update_request_with_output(self, request, new_token_ids, is_stale=False):
        """Publish device proposals without committing guesses into the confirmed prefix."""
        if getattr(request, "dt_reserved_turn", None) is not None:
            request.status = RequestStatus.FINISHED_LENGTH_CAPPED
            return new_token_ids, True
        return super()._update_request_with_output(request, new_token_ids, is_stale)

    def _preempt_request(self, request, timestamp, drop_stale_output=False):
        """Let native pressure reclaim private KV without regenerating an idle proposal."""
        super()._preempt_request(request, timestamp, drop_stale_output)
        if request.dt_waiting:
            request.status = RequestStatus.WAITING_FOR_STREAMING_REQ
            self.num_waiting_for_streaming_input += 1

    def _ready(self, request):
        return not request.dt_waiting and (
            not self.num_spec_tokens or request.num_in_flight_tokens == 0
        )

    def schedule(self, throttle_prefills=False):
        """Batch ready turns while retaining ordinary native allocation and preemption."""
        self._device_batch_active = any(
            getattr(request, "dt_reserved_turn", None) is not None
            for request in self.requests.values()
        )
        for request in self.running:
            if not self._ready(request) or (
                self._device_batch_active
                and getattr(request, "dt_reserved_turn", None) is None
            ):
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
            self._device_batch_active = False
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

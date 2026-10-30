# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retained draft turns layered on native synchronous and asynchronous scheduling."""

import time
from contextlib import contextmanager
from copy import copy
from dataclasses import asdict, dataclass, field
from functools import partial

from vllm import SamplingParams
from vllm.v1.core.sched.async_scheduler import AsyncScheduler
from vllm.v1.core.sched.interface import PauseState
from vllm.v1.core.sched.output import CachedRequestData
from vllm.v1.core.sched.scheduler import Scheduler
from vllm.v1.engine import FinishReason
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
    """Describe a pending device query whose native KV is allocated when selected."""

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
        self.dt_input_resolutions = {}
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
            self._activate_reserved_turn(previous)
            return
        if not request.resumable:
            self.finish_requests(request.request_id, RequestStatus.FINISHED_ABORTED)
            return
        if previous is not None:
            if not previous.dt_waiting:
                raise ValueError(
                    "Draft input arrived before the previous turn finished"
                )
            peer_id = getattr(previous, "dt_peer_id", None)
            if peer_id is not None:
                request.dt_peer_id = peer_id
                extra = dict(request.sampling_params.extra_args or {})
                extra["dt_peer_id"] = peer_id
                request.sampling_params.extra_args = extra
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

    def _activate_reserved_turn(self, request):
        """Resume one retained turn while its inputs await device completion."""
        if not request.dt_waiting or request.num_in_flight_tokens:
            raise ValueError("Device turn arrived before native completion")
        if request.status == RequestStatus.WAITING_FOR_STREAMING_REQ:
            self.num_waiting_for_streaming_input -= 1
        if request not in self.running:
            for queue in self.dt_waiting_queues:
                queue.remove_requests((request,))
            request.status = RequestStatus.WAITING
            self.deferred_waiting.discard(request)
            self._enqueue_waiting_request(request)
        # Speculative private KV remains reusable by the worker, but only the
        # confirmed prefix may count toward native allocation and scheduling.
        request.num_computed_tokens = min(
            request.num_computed_tokens, request.num_prompt_tokens
        )
        request.dt_waiting = False
        request.next_decode_eligible_step = 0
        request.status = (
            RequestStatus.RUNNING if request in self.running else RequestStatus.WAITING
        )

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
            or not set(indexed).issubset(ids)
        ):
            raise ValueError(
                "External admission plan does not match its complete request batch"
            )
        prepared = []
        for plan in plans:
            request_id = plan["request_id"]
            incoming = indexed.get(request_id)
            request = self.requests.get(request_id, incoming)
            if request is None:
                raise ValueError("External admission plan has no native request")
            try:
                turn = ReservedDraftTurn(
                    **{key: value for key, value in plan.items() if key != "request_id"}
                )
            except TypeError as error:
                raise ValueError(
                    "External admission plan has invalid reservation fields"
                ) from error
            if incoming is not None:
                supplied = (incoming.sampling_params.extra_args or {}).get(
                    "dt_reserved_turn"
                )
                try:
                    matches = (
                        supplied is not None and ReservedDraftTurn(**supplied) == turn
                    )
                except TypeError as error:
                    raise ValueError(
                        "External admission request has invalid reservation fields"
                    ) from error
                if not matches or not incoming.resumable:
                    raise ValueError(
                        "External admission request has inconsistent reservation parameters"
                    )
            if not request.resumable:
                raise ValueError(
                    "External admission requires a resumable native request"
                )
            if turn.initial:
                if (
                    incoming is None
                    or request is not incoming
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
            or self.vllm_config.parallel_config.distributed_executor_backend != "uni"
            or self.pause_state != PauseState.UNPAUSED
            or self.need_mamba_block_aligned_split
            or self.num_prefill_lookahead > 1
            or self.connector is not None
            or self.ec_connector is not None
            or self.cache_config.enable_prefix_caching
            or any(
                turn.query_tokens > self.max_num_scheduled_tokens
                or turn.query_tokens
                + self.vllm_config.speculative_config.max_num_new_slots_for_drafting
                > self.scheduler_config.max_num_batched_tokens
                or turn.seq_len_upper_bound + self.num_spec_tokens >= self.max_model_len
                or request.has_encoder_inputs
                or request.mm_features
                or request.lora_request is not None
                or (
                    0
                    < self.scheduler_config.long_prefill_token_threshold
                    < turn.query_tokens
                )
                or request.num_in_flight_tokens
                or (request.request_id in self.requests and not request.dt_waiting)
                for request, turn in prepared
            )
        ):
            yield False
            return
        for request, turn in prepared:
            request.dt_reserved_turn = turn
            request.dt_input_ready = False
            request.dt_pending_confirmation = None
            request.sampling_params.extra_args["dt_reserved_turn"] = asdict(turn)
            if not turn.initial and request.request_id not in indexed:
                # A retained stream has no incoming ADD to activate it. New
                # stream registration activates through add_request instead.
                self._activate_reserved_turn(request)
        # Native selection owns KV allocation; reception does not depend on
        # completion of another peer's compute batch.
        yield True

    def update_external_inputs(self, updates):
        """Activate recorded input revisions on Core's thread after device completion."""
        for request_id, sequence in updates:
            request = self.requests.get(request_id)
            turn = getattr(request, "dt_reserved_turn", None)
            if turn is None or turn.turn_sequence != sequence:
                continue
            request.dt_input_ready = True
            if (
                not turn.initial
                and request.num_computed_tokens < request.num_prompt_tokens
            ):
                self.dt_input_resolutions[request_id] = sequence

    def take_external_input_resolutions(self):
        """Drain ready turns whose private KV requires confirmed-prefix replay."""
        work = []
        for request_id, sequence in list(self.dt_input_resolutions.items()):
            request = self.requests.get(request_id)
            turn = getattr(request, "dt_reserved_turn", None)
            if turn is None or turn.turn_sequence != sequence:
                del self.dt_input_resolutions[request_id]
            elif request.dt_input_ready and not request.num_in_flight_tokens:
                work.append((request_id, sequence))
                del self.dt_input_resolutions[request_id]
        return work

    def update_external_input_metadata(self, metadata):
        """Commit input once, then replay the same native request and turn."""
        for request_id, confirmed in metadata.items():
            request = self.requests.get(request_id)
            turn = getattr(request, "dt_reserved_turn", None)
            if turn is None or turn.turn_sequence != confirmed.turn_sequence:
                continue
            frontier = min(request.num_computed_tokens, request.num_prompt_tokens)
            self._apply_device_confirmation(request, confirmed, frontier)
            request.dt_pending_confirmation = confirmed
            request.dt_reserved_turn = None
            request.sampling_params.extra_args.pop("dt_reserved_turn", None)

    def has_pending_external_inputs(self):
        """Count accepted turns for drain without manufacturing runnable work."""
        return any(
            getattr(request, "dt_reserved_turn", None) is not None
            for request in self.requests.values()
        )

    def get_min_input_tokens(self, request):
        """Preserve one external query as an indivisible native scheduling row."""
        turn = getattr(request, "dt_reserved_turn", None)
        return turn.query_tokens if turn is not None else 0

    def get_num_waiting_tokens(self, request, num_computed_tokens):
        """Use complete external widths for native waiting admission."""
        turn = getattr(request, "dt_reserved_turn", None)
        return (
            turn.query_tokens
            if turn is not None
            else super().get_num_waiting_tokens(request, num_computed_tokens)
        )

    def get_num_allocation_tokens(self, request, num_query_tokens, num_computed_tokens):
        """Reserve a logical upper bound while native scheduling retains KV ownership."""
        turn = getattr(request, "dt_reserved_turn", None)
        return (
            max(1, turn.seq_len_upper_bound - num_computed_tokens)
            if turn is not None
            else num_query_tokens
        )

    def get_num_new_tokens(self, request):
        """Schedule a reserved input shape without appending padding to the confirmed prefix."""
        turn = getattr(request, "dt_reserved_turn", None)
        if turn is not None:
            return turn.query_tokens
        return super().get_num_new_tokens(request)

    def _handle_blocked_waiting_request(self, request):
        if not self._ready(request):
            return False
        if self._device_batch_active != (
            getattr(request, "dt_reserved_turn", None) is not None
        ):
            return False
        return super()._handle_blocked_waiting_request(request)

    def _update_after_schedule(self, output):
        """Consume scheduled confirmations and account for whole neural proposals."""
        # Retained turns and replay can rebind previously allocated blocks.
        # Publish their complete table rather than only the current growth delta.
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
        peer_id = getattr(session, "dt_peer_id", None)
        if peer_id is not None:
            extra = dict(update.sampling_params.extra_args or {})
            extra["dt_peer_id"] = peer_id
            update.sampling_params.extra_args = extra

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

    def _apply_device_confirmation(self, request, confirmed, materialized_frontier):
        """Commit input once without overstating physical KV materialization."""
        turn = request.dt_reserved_turn
        if turn is None or turn.turn_sequence != confirmed.turn_sequence:
            raise ValueError("Device confirmation does not match the scheduled turn")
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
        request.num_computed_tokens = materialized_frontier
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

    @staticmethod
    def _confirmation_payload(confirmed):
        return {
            "sequence": confirmed.turn_sequence,
            "confirmed_length": confirmed.confirmed_length,
            "context_length": confirmed.context_length,
            "anchor": confirmed.anchor,
            "token_ids_delta": confirmed.token_ids_delta,
        }

    def update_from_output(self, scheduler_output, model_runner_output):
        """Retain parked turns after native stop handling removes them from the running list."""
        completed = {}
        for request_id, confirmed in getattr(
            model_runner_output, "confirmed_turns", {}
        ).items():
            request = self.requests.get(request_id)
            if request is None or request.is_finished():
                continue
            frontier = (
                0
                if request_id in self.dt_input_resolutions
                else confirmed.context_length
            )
            self._apply_device_confirmation(request, confirmed, frontier)
            self.dt_input_resolutions.pop(request_id, None)
            completed[request_id] = self._confirmation_payload(confirmed)
        for request_id in scheduler_output.num_scheduled_tokens:
            request = self.requests.get(request_id)
            pending = getattr(request, "dt_pending_confirmation", None)
            if pending is not None:
                completed[request_id] = self._confirmation_payload(pending)
        outputs = super().update_from_output(scheduler_output, model_runner_output)
        for client_outputs in outputs.values():
            for output in client_outputs.outputs:
                confirmation = completed.get(output.request_id)
                if (
                    confirmation is not None
                    and output.finish_reason == FinishReason.LENGTH
                ):
                    output.finish_reason = None
                    output.continuation_output = confirmation
                    self.requests[output.request_id].dt_pending_confirmation = None
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
        """Retain accepted input while native pressure reclaims private KV."""
        super()._preempt_request(request, timestamp, drop_stale_output)
        turn = getattr(request, "dt_reserved_turn", None)
        if turn is not None and (not turn.initial or request.num_in_flight_tokens):
            # A first prompt can restart directly before execution. Once its
            # output is in flight, confirmation must not resurrect reclaimed KV.
            self.dt_input_resolutions[request.request_id] = turn.turn_sequence
        if request.dt_waiting:
            request.status = RequestStatus.WAITING_FOR_STREAMING_REQ
            self.num_waiting_for_streaming_input += 1

    def _ready(self, request):
        turn = getattr(request, "dt_reserved_turn", None)
        return (
            not request.dt_waiting
            and not request.num_in_flight_tokens
            and (
                turn is None
                or (
                    getattr(request, "dt_input_ready", False)
                    and request.request_id not in self.dt_input_resolutions
                )
            )
        )

    @staticmethod
    def _draft_peer_id(request):
        """Return the stable Draft affinity for a request, if a pooled Target assigned one."""
        peer_id = getattr(request, "dt_peer_id", None)
        if peer_id is not None:
            return peer_id
        turn = getattr(request, "dt_reserved_turn", None)
        if turn is not None:
            return turn.peer_id
        return (request.sampling_params.extra_args or {}).get("dt_peer_id")

    def schedule(self, throttle_prefills=False):
        """Batch compatible ready rows under native arrival and priority policy."""
        ready = [request for request in self.requests.values() if self._ready(request)]
        priority = getattr(self.policy, "value", self.policy) == "priority"
        # Native scheduling considers resident rows before waiting admission.
        # Selecting a waiting input class while incompatible ready rows fill
        # every slot would prevent both classes from making progress.
        selected = next(
            (request for request in self.running if self._ready(request)),
            None,
        )
        if selected is None:
            selected = min(
                ready,
                key=lambda request: (
                    request.priority if priority else 0,
                    request.arrival_time,
                    request.request_id,
                ),
                default=None,
            )
        selected_peer_id = self._draft_peer_id(selected) if selected is not None else None
        self._device_batch_active = (
            selected is not None
            and getattr(selected, "dt_reserved_turn", None) is not None
        )
        for request in self.requests.values():
            if (
                not self._ready(request)
                or self._device_batch_active
                != (getattr(request, "dt_reserved_turn", None) is not None)
                or (
                    selected_peer_id is not None
                    and self._draft_peer_id(request) != selected_peer_id
                )
            ):
                request.next_decode_eligible_step = self.current_step + 2
            else:
                request.next_decode_eligible_step = 0
        try:
            output = super().schedule(throttle_prefills)
            waiting_ready = sum(
                self._ready(request)
                for request in self.requests.values()
                if request not in self.running
            )
            if not output.total_num_scheduled_tokens and waiting_ready:
                # Parked turns retain slots and KV even though they have no
                # work. Reclaim them through native preemption so a partial
                # peer batch can finish without waiting for its own next turn.
                available = self.max_num_running_reqs - len(self.running)
                to_reclaim = max(
                    1, min(waiting_ready, self.max_num_running_reqs) - available
                )
                idle = sorted(self.running, key=lambda request: not request.dt_waiting)
                for request in idle:
                    pending_input = getattr(
                        request, "dt_reserved_turn", None
                    ) is not None and not getattr(request, "dt_input_ready", False)
                    if (
                        (request.dt_waiting or pending_input)
                        and not request.num_in_flight_tokens
                        and self._request_blocks_can_be_freed(request)
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
                    self.deferred_waiting.discard(request)
                    if request.status == RequestStatus.WAITING_FOR_STREAMING_REQ:
                        self.num_waiting_for_streaming_input -= 1
                    request.status = RequestStatus.PREEMPTED

    def has_requests(self):
        """Sleep until readiness or native outputs make a real execution step possible."""
        if self.pause_state == PauseState.PAUSED_ALL:
            return self.has_finished_requests()
        return self.has_finished_requests() or any(
            self._ready(request) for request in self.requests.values()
        )


class DraftScheduler(DraftTurns, Scheduler):
    """Use native synchronous scheduling for retained Draft turns."""


class AsyncDraftScheduler(DraftTurns, AsyncScheduler):
    """Use native asynchronous batching without predicting a second unconfirmed turn."""

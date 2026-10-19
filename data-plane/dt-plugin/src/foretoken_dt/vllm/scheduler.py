# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""External candidate admission layered on vLLM's scheduling and KV lifecycle."""

import time
from importlib.metadata import version
from uuid import uuid4

from vllm.v1.core.sched.interface import PauseState
from vllm.v1.core.sched.output import SchedulerOutput
from vllm.v1.core.sched.scheduler import Scheduler
from vllm.v1.engine import EngineCoreOutputs
from vllm.v1.outputs import ModelRunnerOutput
from vllm.v1.request import Request, RequestStatus

from . import METAX_VLLM_VERSION


class ObservedScheduler(Scheduler):
    """Measure native scheduling; AsyncLLM consumes observations and owns their cleanup."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dt_observations = {}
        self.dt_instance = uuid4().hex
        self.dt_batch_sequence = 0
        # MetaX retains the 0.30 core; 0.31 splits blocked requests from KV holders.
        self.dt_waiting_queues = (
            self.waiting,
            self.skipped_waiting
            if version("vllm") == METAX_VLLM_VERSION
            else self.kv_holding_waiting,
        )

    def add_request(self, request: Request) -> None:
        """Start timing at native admission, independently of HTTP session lifetime."""
        identity = (request.sampling_params.extra_args or {}).get("dt_work")
        self.dt_observations[request.request_id] = {
            "ready_at": time.monotonic(),
            "remote_since": None,
            "totals": {
                "queue_seconds": 0.0,
                "scheduled_seconds": 0.0,
                "remote_wait_seconds": 0.0,
                "scheduled_tokens": 0,
                "verified_tokens": 0,
                "accepted_tokens": 0,
                "steps": 0,
                "preemptions": 0,
            },
            "outputs": {},
            "physical_batches": [],
            "work": identity,
            "client_request_id": identity["request_id"] if identity else None,
            "initial_frontier": identity["frontier"] if identity else None,
        }
        super().add_request(request)

    def schedule(self, throttle_prefills: bool = False) -> SchedulerOutput:
        """Measure time ready work waits, then time each shared native execution step."""
        output = super().schedule(throttle_prefills)
        now = time.monotonic()
        output.dt_started_at = now
        output.dt_started_at_unix_ms = time.time_ns() // 1_000_000
        self.dt_batch_sequence += 1
        output.dt_batch_id = f"{self.dt_instance}:{self.dt_batch_sequence}"
        output.dt_members = []
        for req_id, count in output.num_scheduled_tokens.items():
            record = self.dt_observations[req_id]
            # Native abort may remove the request while this forward is in flight.
            # Capture membership/frontier at scheduling, alongside token accounting.
            output.dt_members.append(
                {
                    "engine_request_id": req_id,
                    "request_id": record["client_request_id"],
                    "confirmed_frontier": (
                        record["initial_frontier"]
                        + (
                            self.requests[req_id].num_output_tokens
                            if isinstance(self, ExternalScheduler)
                            else 0
                        )
                        if record["initial_frontier"] is not None
                        else None
                    ),
                    "work": record["work"],
                    "scheduled_tokens": count,
                }
            )
            if record["ready_at"] is not None:
                record["totals"]["queue_seconds"] += now - record["ready_at"]
            record["ready_at"] = None
            record["totals"]["scheduled_tokens"] += count
        return output

    def update_from_output(self, scheduler_output, model_runner_output):
        """Capture sampler acceptance before stop truncation and post-output KV residency."""
        now = time.monotonic()
        batch = {
            "batch_id": scheduler_output.dt_batch_id,
            "started_at_unix_ms": scheduler_output.dt_started_at_unix_ms,
            "finished_at_unix_ms": time.time_ns() // 1_000_000,
            "scheduled_tokens": scheduler_output.total_num_scheduled_tokens,
            "scheduled_seconds": now - scheduler_output.dt_started_at,
            "members": scheduler_output.dt_members,
        }
        for req_id in scheduler_output.num_scheduled_tokens:
            # Match native update_from_output's abort handling. The physical batch
            # retains the scheduled member even if its session no longer consumes output.
            if req_id not in self.requests:
                continue
            self.dt_observations[req_id]["physical_batches"].append(batch)
            record = self.dt_observations[req_id]
            totals = record["totals"]
            totals["scheduled_seconds"] += now - scheduler_output.dt_started_at
            totals["steps"] += 1
            candidates = scheduler_output.scheduled_spec_decode_tokens.get(req_id, [])
            if candidates:
                index = model_runner_output.req_id_to_index[req_id]
                sampled = model_runner_output.sampled_token_ids[index]
                totals["verified_tokens"] += len(candidates)
                # Native rejection sampling returns accepted candidates plus one
                # correction/bonus token. Count before EOS/max_tokens truncation.
                totals["accepted_tokens"] += max(len(sampled) - 1, 0)
            record["ready_at"] = now
        requests = {
            key: self.requests[key]
            for key in scheduler_output.num_scheduled_tokens
            if key in self.requests
        }
        outputs = super().update_from_output(scheduler_output, model_runner_output)
        for client_output in outputs.values():
            for output in client_output.outputs:
                if not output.new_token_ids:
                    continue
                req_id = output.request_id
                record = self.dt_observations[req_id]
                if not isinstance(self, ExternalScheduler):
                    # Draft consumes one cumulative observation per completed turn.
                    record["outputs"].clear()
                record["outputs"][requests[req_id].num_output_tokens] = {
                    **record["totals"],
                    "batch_requests": len(scheduler_output.num_scheduled_tokens),
                    "batch_tokens": scheduler_output.total_num_scheduled_tokens,
                    "kv": self.dt_request_kv(req_id),
                    "physical_batches": list(record["physical_batches"]),
                }
        return outputs

    def _preempt_request(self, request, timestamp, drop_stale_output=False):
        """Count native preemption while native code retains KV release ownership."""
        record = self.dt_observations[request.request_id]
        record["totals"]["preemptions"] += 1
        if record["remote_since"] is None:
            record["ready_at"] = time.monotonic()
        return super()._preempt_request(request, timestamp, drop_stale_output)

    def dt_request_kv(self, request_id):
        """Report live request pages; shared pages count here but are not exclusive bytes."""
        from vllm.v1.kv_cache_interface import UniformTypeKVCacheSpecs

        request = self.requests.get(request_id)
        if request is None:
            return None
        manager = self.kv_cache_manager
        blocks = manager.get_blocks(request_id).blocks
        groups = manager.kv_cache_config.kv_cache_groups
        allocations = []
        for group, pages in zip(groups, blocks, strict=True):
            spec = group.kv_cache_spec
            page_bytes = (
                spec.page_size_bytes
                if isinstance(spec, UniformTypeKVCacheSpecs)
                else spec.page_size_bytes * len(group.layer_names)
            )
            count = sum(not block.is_null for block in pages)
            allocations.append(
                {
                    "block_tokens": spec.block_size,
                    "allocated_blocks": count,
                    "allocated_bytes": count * page_bytes,
                }
            )
        return {
            "context_tokens": request.num_tokens,
            "computed_tokens": request.num_computed_tokens,
            "groups": allocations,
        }

    def dt_workload(self):
        """Read one native scheduler/KV snapshot for online placement, without Prometheus."""
        waiting = [request for queue in self.dt_waiting_queues for request in queue]
        remote = [
            r
            for r in self.running
            if (
                getattr(r, "waiting_for_external_draft", False)
                or getattr(r, "dt_draft_waiting", False)
            )
            and not getattr(r, "external_draft_recovering", False)
        ]
        ready = [r for r in self.running if r not in remote]
        manager = self.kv_cache_manager
        pool = manager.block_pool
        # vLLM reserves one null block; free cached blocks are reclaimable capacity.
        capacity = pool.num_gpu_blocks - 1
        return {
            "collected_at_unix_ms": time.time_ns() // 1_000_000,
            "running_requests": len(self.running),
            "ready_requests": len(ready),
            "remote_waiting_requests": len(remote),
            "waiting_requests": len(waiting),
            "waiting_tokens": sum(
                max(r.num_tokens - r.num_computed_tokens, 0) for r in waiting
            ),
            "ready_tokens": sum(
                max(r.num_tokens + len(r.spec_token_ids) - r.num_computed_tokens, 0)
                for r in ready
            ),
            "max_num_seqs": self.scheduler_config.max_num_seqs,
            "max_num_batched_tokens": self.scheduler_config.max_num_batched_tokens,
            "kv_cache_usage": manager.usage,
            "kv_capacity_blocks": capacity,
            "kv_used_blocks": capacity - pool.get_num_free_blocks(),
        }

    def dt_take_observation(self, request_id, generation, finished):
        """Consume one output's cumulative measurements, retaining no completed history."""
        outputs = self.dt_observations[request_id]["outputs"]
        key = max(outputs) if finished else generation
        result = dict(outputs[key])
        consumed_batches = {batch["batch_id"] for batch in result["physical_batches"]}
        record = self.dt_observations[request_id]
        record["physical_batches"] = [
            batch
            for batch in record["physical_batches"]
            if batch["batch_id"] not in consumed_batches
        ]
        for pending in outputs.values():
            pending["physical_batches"] = [
                batch
                for batch in pending["physical_batches"]
                if batch["batch_id"] not in consumed_batches
            ]
        for consumed in [value for value in outputs if value <= key]:
            del outputs[consumed]
        return result


class DraftScheduler(ObservedScheduler):
    """Retain private native Draft KV between confirmed streaming-input turns."""

    def __init__(self, *args, **kwargs):
        """Keep resumed neural input bounded by confirmed features and the native method budget."""
        super().__init__(*args, **kwargs)
        if self.num_spec_tokens:
            # A resumed neural turn can need just one new confirmed feature.
            # Disable native static padding with synthetic speculative inputs;
            # those positions have no Target features. Keep the method's budget.
            self.dynamic_sd_lookup = [self.num_spec_tokens] * (
                self.scheduler_config.max_num_seqs + 1
            )

    def add_request(self, request):
        """Admit a first turn or replace a paused turn with the exact confirmed prefix."""
        previous = self.requests.get(request.request_id)
        if not request.resumable:
            # Native streaming input sends a non-resumable sentinel on closure,
            # including when preprocessing rejected its very first input chunk.
            self.finish_requests(request.request_id, RequestStatus.FINISHED_ABORTED)
            return
        if previous is not None:
            if not previous.dt_draft_waiting:
                raise ValueError(
                    "Draft input arrived before its previous turn completed"
                )
            self.running = [r for r in self.running if r is not previous]
            for queue in self.dt_waiting_queues:
                queue.remove_requests((previous,))
            # Native streaming admission applies the update immediately only in
            # this status. This transient counter is consumed by its update hook.
            if previous.status != RequestStatus.WAITING_FOR_STREAMING_REQ:
                previous.status = RequestStatus.WAITING_FOR_STREAMING_REQ
                self.num_waiting_for_streaming_input += 1
        else:
            request.dt_draft_waiting = False
        super().add_request(request)
        if previous is not None:
            # The previous slot must be rebuilt with the updated prompt, sampler
            # and feature binding, while retaining the scheduler's physical pages.
            previous.dt_draft_waiting = False
            self._enqueue_waiting_request(previous)

    def _update_request_as_session(self, session, update):
        """Discard unconfirmed suffix state before native streaming input resumes KV."""
        prompt = update.prompt_token_ids
        extra = update.sampling_params.extra_args
        old_extra = session.sampling_params.extra_args
        if extra["dt_version"] <= old_extra["dt_version"]:
            raise ValueError("Draft turn did not advance its confirmed frontier")
        old_prompt = session.prompt_token_ids
        if prompt[: len(old_prompt)] != old_prompt:
            raise ValueError("Draft confirmed input changed an earlier prefix")
        reused = 0
        for old, new in zip(session.all_token_ids, prompt):
            if old != new:
                break
            reused += 1
        reused = min(reused, session.num_computed_tokens)
        if self.vllm_config.additional_config.get("foretoken_dt_context_role"):
            # Neural proposal KV is conditioned on provisional features/anchors.
            # Only rows computed from the previous confirmed Target context are
            # reusable; its last row also requires the same next-token anchor.
            old_confirmed = [*old_prompt, old_extra["dt_anchor_token"]]
            new_confirmed = [*prompt, extra["dt_anchor_token"]]
            if new_confirmed[: len(old_confirmed)] != old_confirmed:
                raise ValueError("Draft anchor changed a confirmed input")
            reused = min(reused, len(old_prompt))
        session.num_computed_tokens = reused
        del session._all_token_ids[reused:]
        session._output_token_ids.clear()
        session.prompt_token_ids[:] = prompt[:reused]
        session.num_prompt_tokens = reused
        session.spec_token_ids = []
        session.block_hashes.clear()
        update.prompt_token_ids = prompt[reused:]
        super()._update_request_as_session(session, update)
        session.max_tokens = update.max_tokens
        session.next_decode_eligible_step = 0

    def _handle_stopped_request(self, request):
        """Park a completed turn as a preemptible resident, without ending the session."""
        request.dt_draft_waiting = True
        request.status = RequestStatus.RUNNING
        record = self.dt_observations[request.request_id]
        record["ready_at"] = None
        record["remote_since"] = time.monotonic()
        return False

    def update_from_output(self, scheduler_output, model_runner_output):
        """Restore parked turns removed from running by native turn-stop handling."""
        outputs = super().update_from_output(scheduler_output, model_runner_output)
        for request_id in scheduler_output.num_scheduled_tokens:
            request = self.requests.get(request_id)
            if (
                request is not None
                and request.dt_draft_waiting
                and request not in self.running
            ):
                self.running.append(request)
        return outputs

    def _preempt_request(self, request, timestamp, drop_stale_output=False):
        """Let native pressure release parked KV without replaying an unconfirmed turn."""
        super()._preempt_request(request, timestamp, drop_stale_output)
        if request.dt_draft_waiting:
            # Native may inspect the victim again during this schedule call.
            # Block it until the confirmed next input arrives, rather than
            # regenerating its already returned candidates after preemption.
            request.status = RequestStatus.WAITING_FOR_STREAMING_REQ
            self.num_waiting_for_streaming_input += 1

    def schedule(self, throttle_prefills=False):
        """Batch ready turns natively while parked turns retain preemptible KV."""
        for request in self.running:
            if request.dt_draft_waiting:
                request.next_decode_eligible_step = self.current_step + 2
        try:
            return super().schedule(throttle_prefills)
        finally:
            # Evicted parked turns own no KV or runner slot. Leave their logical
            # request in the native request table, outside admission queues.
            for request in self.requests.values():
                if request.status == RequestStatus.WAITING_FOR_STREAMING_REQ:
                    for queue in self.dt_waiting_queues:
                        queue.remove_requests((request,))
                    self.num_waiting_for_streaming_input -= 1
                    request.status = RequestStatus.PREEMPTED

    def has_schedulable_requests(self):
        """Sleep between turns; input, abort and other ready requests wake EngineCore."""
        if self.pause_state == PauseState.PAUSED_ALL:
            return self.has_finished_requests()
        return bool(
            self.has_finished_requests()
            or any(not r.dt_draft_waiting for r in self.running)
            or (
                self.pause_state == PauseState.UNPAUSED
                and len(self.running) < self.scheduler_config.max_num_seqs
                and any(self.dt_waiting_queues)
            )
        )


class ExternalScheduler(ObservedScheduler):
    """Keep remote waiters resident and preemptible while other requests run."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # The native dynamic-budget path disables synthetic draft padding. Remote
        # candidates must have real IDs and matching proposal distributions.
        self.dynamic_sd_lookup = [self.num_spec_tokens] * (
            self.scheduler_config.max_num_seqs + 1
        )

    def add_request(self, request: Request) -> None:
        """Initialize admission state on the request whose lifetime vLLM owns."""
        request.waiting_for_external_draft = False
        request.external_draft_recovering = False
        # None means no admitted chain; [] is an admitted Target-only step.
        request.external_draft_tokens = None
        super().add_request(request)

    def schedule(self, throttle_prefills: bool = False) -> SchedulerOutput:
        """Reuse native batching, KV allocation and preemption for ready requests."""
        for request in self.running:
            if request.external_draft_recovering:
                request.next_decode_eligible_step = 0
            elif request.waiting_for_external_draft:
                # Native schedule increments current_step before checking decode
                # eligibility. Keep waiters in running so they remain KV victims.
                request.next_decode_eligible_step = self.current_step + 2
            elif request.external_draft_tokens is not None:
                request.spec_token_ids = list(request.external_draft_tokens)
        output = super().schedule(throttle_prefills)
        # Replay has no logical output. The final replay step leaves the known
        # anchor uncomputed so the original chain can verify from that position.
        output.external_draft_recovery = {
            request_id: self.requests[request_id].num_computed_tokens
            >= self.requests[request_id].num_tokens
            for request_id in output.num_scheduled_tokens
            if self.requests[request_id].external_draft_recovering
        }
        output.external_draft_generations = {
            request_id: self.requests[request_id].num_output_tokens
            for request_id in output.scheduled_spec_decode_tokens
        }
        return output

    def update_from_output(
        self,
        scheduler_output: SchedulerOutput,
        model_runner_output: ModelRunnerOutput,
    ) -> dict[int, EngineCoreOutputs]:
        """Pause surviving token-producing requests after native stop processing."""
        outputs = super().update_from_output(scheduler_output, model_runner_output)
        for request_id, complete in scheduler_output.external_draft_recovery.items():
            request = self.requests.get(request_id)
            if request is not None and complete:
                request.num_computed_tokens -= 1
                request.external_draft_recovering = False
                if request.waiting_for_external_draft:
                    self.dt_observations[request_id]["ready_at"] = None
        for client_output in outputs.values():
            for output in client_output.outputs:
                request = self.requests.get(output.request_id)
                if (
                    request is not None
                    and not request.is_finished()
                    and output.new_token_ids
                ):
                    request.waiting_for_external_draft = True
                    request.external_draft_tokens = None
                    record = self.dt_observations[output.request_id]
                    record["ready_at"] = None
                    record["remote_since"] = time.monotonic()
        return outputs

    def _preempt_request(
        self, request: Request, timestamp: float, drop_stale_output: bool = False
    ) -> None:
        """Recover physical KV without consuming the outstanding logical round."""
        request.external_draft_recovering = request.num_output_tokens > 0
        request.next_decode_eligible_step = 0
        super()._preempt_request(request, timestamp, drop_stale_output)

    def submit_external_draft_tokens(
        self,
        request_id: str,
        generation: int,
        token_ids: list[int],
        work: dict | None = None,
    ) -> bool:
        """Admit one ready round on EngineCore's loop; reject stale or closed tickets."""
        request = self.requests.get(request_id)
        if (
            request is None
            or request.is_finished()
            or not request.waiting_for_external_draft
            or request.num_output_tokens != generation
        ):
            return False
        if len(token_ids) > self.num_spec_tokens:
            raise ValueError("candidate length exceeds num_speculative_tokens")
        vocab_size = self.vllm_config.model_config.get_vocab_size()
        if any(
            type(token) is not int or not 0 <= token < vocab_size for token in token_ids
        ):
            raise ValueError("external candidates must be target vocabulary token IDs")
        record = self.dt_observations[request_id]
        now = time.monotonic()
        record["totals"]["remote_wait_seconds"] += now - record["remote_since"]
        record["remote_since"] = None
        record["ready_at"] = now
        record["work"] = work
        # Keep the accepted chain across native preemption, which clears spec IDs.
        # Only populate native IDs once replay has restored the confirmed KV.
        request.external_draft_tokens = list(token_ids)
        if not request.external_draft_recovering:
            request.spec_token_ids = list(token_ids)
        request.waiting_for_external_draft = False
        request.next_decode_eligible_step = 0
        return True

    def has_schedulable_requests(self) -> bool:
        """Let EngineCore sleep until remote input or native admission can progress."""
        if self.pause_state == PauseState.PAUSED_ALL:
            return self.has_finished_requests()
        # Match native waiting-to-running admission, including slots retained by
        # streaming input. A full set of remote waiters cannot admit queued work;
        # candidate submission and abort wake EngineCore through its input queue.
        can_admit = (
            self.pause_state == PauseState.UNPAUSED
            and len(self.running) + self.num_waiting_for_streaming_input
            < self.scheduler_config.max_num_seqs
        )
        return bool(
            (can_admit and any(self.dt_waiting_queues))
            or self.has_finished_requests()
            or any(
                request.external_draft_recovering
                or not request.waiting_for_external_draft
                for request in self.running
            )
        )

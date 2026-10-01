# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""External candidate admission layered on vLLM's scheduling and KV lifecycle."""

from vllm.v1.core.sched.output import SchedulerOutput
from vllm.v1.core.sched.scheduler import Scheduler
from vllm.v1.engine import EngineCoreOutputs
from vllm.v1.outputs import ModelRunnerOutput
from vllm.v1.request import Request


class ExternalScheduler(Scheduler):
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
        super().add_request(request)

    def schedule(self, throttle_prefills: bool = False) -> SchedulerOutput:
        """Reuse native batching, KV allocation and preemption for ready requests."""
        for request in self.running:
            if request.waiting_for_external_draft:
                # Native schedule increments current_step before checking decode
                # eligibility. Keep waiters in running so they remain KV victims.
                request.next_decode_eligible_step = self.current_step + 2
        output = super().schedule(throttle_prefills)
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
        for client_output in outputs.values():
            for output in client_output.outputs:
                request = self.requests.get(output.request_id)
                if (
                    request is not None
                    and not request.is_finished()
                    and output.new_token_ids
                ):
                    request.waiting_for_external_draft = True
        return outputs

    def _preempt_request(
        self, request: Request, timestamp: float, drop_stale_output: bool = False
    ) -> None:
        """Invalidate remote admission before native KV release and prompt replay."""
        request.waiting_for_external_draft = False
        request.next_decode_eligible_step = 0
        super()._preempt_request(request, timestamp, drop_stale_output)

    def submit_external_draft_tokens(
        self, request_id: str, generation: int, token_ids: list[int]
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
        request.spec_token_ids = list(token_ids)
        request.waiting_for_external_draft = False
        request.next_decode_eligible_step = 0
        return True

    def has_schedulable_requests(self) -> bool:
        """Let EngineCore sleep when only live remote waiters remain."""
        return bool(
            self.waiting
            or self.skipped_waiting
            or self.has_finished_requests()
            or any(not request.waiting_for_external_draft for request in self.running)
        )

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Bind pre-scheduled Draft rows to Target-produced device inputs and feedback."""

from dataclasses import dataclass

import torch
from vllm.v1.worker.gpu.async_utils import stream


class DeviceInputBatch:
    """Retain one peer's logical batch until native completion retires its readers."""

    def __init__(
        self,
        sequence,
        packed,
        features,
        num_tokens,
        num_requests,
        ready,
        *,
        feedback_packed,
        feedback_features,
    ):
        self.sequence = sequence
        self.packed = packed
        self.features = features
        self.ready = ready
        self.feedback_packed = feedback_packed
        self.feedback_features = feedback_features
        self.input_ids, self.positions, metadata = packed.split(
            (num_tokens, num_tokens, 5 * num_requests)
        )
        self.metadata = metadata.view(5, num_requests)
        (
            self.seq_lens,
            self.num_sampled,
            self.num_rejected,
            self.last_sampled,
            self.total_len,
        ) = self.metadata.unbind(dim=0)


@dataclass
class DeviceInputRow:
    """Map a native request slot to its retained peer row and padded query interval."""

    batch: DeviceInputBatch
    row: int
    offset: int
    count: int
    seq_len_upper_bound: int
    initial: bool = False


class DeviceTurnFeedback:
    """Copy confirmation and replay history alongside native asynchronous output."""

    def __init__(self, runner, rows):
        self.rows = rows
        self.values = []
        with stream(runner.output_copy_stream, runner.main_stream):
            runner.output_copy_stream.wait_stream(runner.main_stream)
            for request_id, binding in rows:
                batch = binding.batch
                runner.output_copy_stream.wait_event(batch.ready)
                num_tokens = batch.input_ids.numel()
                input_ids, positions, metadata = batch.feedback_packed.split(
                    (num_tokens, num_tokens, 5 * batch.metadata.shape[1])
                )
                metadata = metadata.view_as(batch.metadata)
                left, right = binding.offset, binding.offset + binding.count
                input_ids[left:right].copy_(
                    batch.input_ids[left:right], non_blocking=True
                )
                positions[left:right].copy_(
                    batch.positions[left:right], non_blocking=True
                )
                metadata[:, binding.row].copy_(
                    batch.metadata[:, binding.row], non_blocking=True
                )
                features = batch.feedback_features[left:right]
                features.copy_(
                    batch.features[:, left:right].movedim(0, 1), non_blocking=True
                )
                self.values.append(
                    (
                        request_id,
                        (
                            input_ids[left:right].numpy(),
                            positions[left:right].numpy(),
                            metadata[:, binding.row].numpy(),
                            features,
                        ),
                    )
                )

    @classmethod
    def materialize(cls, runner, rows):
        """Confirm ready input once when native KV pressure requires CPU prefix replay."""
        from .scheduler import ConfirmedDeviceTurn

        feedback = cls(runner, rows)
        completed = torch.cuda.Event()
        completed.record(runner.output_copy_stream)
        completed.synchronize()
        return feedback.settle(runner.contexts, ConfirmedDeviceTurn)

    def settle(self, contexts, confirmation_type):
        """Return native-request confirmations after the output copy event has completed."""
        confirmations = {}
        for (_, binding), values in zip(self.rows, self.values, strict=True):
            request_id, (input_ids, positions, metadata, features) = values
            _, sampled, rejected, anchor, total_len = map(int, metadata)
            valid = binding.count - rejected
            if binding.initial:
                valid_confirmation = (
                    sampled == 1 and rejected == 0 and total_len == binding.count + 1
                )
            else:
                valid_confirmation = sampled == valid
            if not valid_confirmation or not 0 < valid <= binding.count:
                raise RuntimeError(
                    "Device confirmation differs from its scheduled query"
                )
            context = contexts[request_id]
            if int(positions[0]) != context.end:
                raise RuntimeError(
                    "Device confirmation skipped its retained feature prefix"
                )
            context_length = total_len - 1
            if context.end + valid != context_length:
                raise RuntimeError(
                    "Device confirmation has an inconsistent feature frontier"
                )
            delta = (
                [anchor] if binding.initial else input_ids[1:valid].tolist() + [anchor]
            )
            context.append(context.end, features[:valid])
            confirmations[request_id] = confirmation_type(
                turn_sequence=binding.batch.sequence,
                token_ids_delta=delta,
                confirmed_length=total_len,
                context_length=context_length,
                anchor=anchor,
                initial=binding.initial,
            )
        return confirmations

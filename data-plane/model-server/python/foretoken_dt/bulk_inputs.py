# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Assemble peer device rows into the native runner's stable input buffers."""

import numpy as np
import torch
from vllm.triton_utils import tl, triton
from vllm.v1.worker.gpu.buffer_utils import UvaBufferPool


class DeviceInputAssembler:
    """Own native-concurrency descriptor buffers and small device confirmation arrays."""

    def __init__(self, max_requests, max_tokens, device):
        self.descriptors = UvaBufferPool((max_requests, 9), dtype=torch.int64)
        self.token_rows = UvaBufferPool(max_tokens, dtype=torch.int32)
        self.sampled = torch.empty(max_requests, dtype=torch.int32, device=device)
        self.rejected = torch.empty_like(self.sampled)

    def assemble(self, runner, batch, rows):
        """Gather all selected peer rows with one device launch and one host descriptor table."""
        descriptors = np.empty((len(rows), 9), dtype=np.int64)
        for row, (_, binding) in enumerate(rows):
            source = binding.batch
            descriptors[row] = (
                source.packed.data_ptr(),
                source.features.data_ptr(),
                source.features.stride(0),
                source.input_ids.numel(),
                source.metadata.shape[1],
                binding.offset,
                binding.row,
                int(batch.query_start_loc_np[row]),
                int(batch.idx_mapping_np[row]),
            )
        descriptor_view = self.descriptors.copy_to_uva(descriptors)
        token_rows = np.repeat(
            np.arange(len(rows), dtype=np.int32), batch.num_scheduled_tokens
        )
        token_row_view = self.token_rows.copy_to_uva(token_rows)
        features = runner.context_features
        width = features.shape[1] * features.shape[2]
        _assemble_inputs[(batch.num_tokens_after_padding, triton.cdiv(width, 1024))](
            descriptor_view,
            token_row_view,
            batch.input_ids,
            batch.positions,
            batch.seq_lens,
            features,
            runner.req_states.last_sampled_tokens,
            self.sampled,
            self.rejected,
            batch.num_tokens,
            runner.req_states.last_sampled_tokens.stride(0),
            runner.req_states.last_sampled_tokens.stride(1),
            HIDDEN=features.shape[2],
            FEATURE_WIDTH=width,
            ANCHOR_WIDTH=runner.req_states.last_sampled_tokens.shape[1],
            ANCHOR_BLOCK=triton.next_power_of_2(
                runner.req_states.last_sampled_tokens.shape[1]
            ),
            BLOCK=1024,
        )


@triton.jit
def _assemble_inputs(
    descriptors,
    token_rows,
    input_ids,
    positions,
    seq_lens,
    features,
    anchors,
    sampled,
    rejected,
    num_tokens,
    anchor_row_stride,
    anchor_column_stride,
    HIDDEN: tl.constexpr,
    FEATURE_WIDTH: tl.constexpr,
    ANCHOR_WIDTH: tl.constexpr,
    ANCHOR_BLOCK: tl.constexpr,
    BLOCK: tl.constexpr,
):
    token = tl.program_id(0)
    feature_block = tl.program_id(1)
    valid = token < num_tokens
    row = tl.load(token_rows + token, valid, other=0)
    entry = descriptors + row * 9
    packed = tl.load(entry).to(tl.pointer_type(tl.int64))
    source_features = tl.load(entry + 1).to(tl.pointer_type(features.dtype.element_ty))
    feature_plane_stride = tl.load(entry + 2)
    source_tokens = tl.load(entry + 3)
    source_rows = tl.load(entry + 4)
    source_start = tl.load(entry + 5)
    source_row = tl.load(entry + 6)
    target_start = tl.load(entry + 7)
    slot = tl.load(entry + 8)
    source_token = source_start + token - target_start
    indices = feature_block * BLOCK + tl.arange(0, BLOCK)
    source_offset = (
        (indices // HIDDEN) * feature_plane_stride
        + source_token * HIDDEN
        + indices % HIDDEN
    )
    values = tl.load(
        source_features + source_offset,
        valid & (indices < FEATURE_WIDTH),
        other=0,
    )
    tl.store(
        features + token * FEATURE_WIDTH + indices, values, indices < FEATURE_WIDTH
    )
    if feature_block == 0:
        value = tl.load(packed + source_token, valid, other=0)
        position = tl.load(packed + source_tokens + source_token, valid, other=0)
        tl.store(input_ids + token, value, valid)
        tl.store(positions + token, position, valid)
        if valid & (token == target_start):
            metadata = packed + 2 * source_tokens + source_row
            tl.store(seq_lens + row, tl.load(metadata))
            tl.store(sampled + row, tl.load(metadata + source_rows))
            tl.store(rejected + row, tl.load(metadata + 2 * source_rows))
            anchor_columns = tl.arange(0, ANCHOR_BLOCK)
            tl.store(
                anchors
                + slot * anchor_row_stride
                + anchor_columns * anchor_column_stride,
                tl.load(metadata + 3 * source_rows),
                anchor_columns < ANCHOR_WIDTH,
            )

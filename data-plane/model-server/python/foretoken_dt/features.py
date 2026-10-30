# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Capture proposal features at their native production points into stable transfer buffers."""

from concurrent.futures import Future

import torch
from vllm.forward_context import get_forward_context
from vllm.model_executor.models.interfaces import EagleModelMixin

_CAPTURE_LAYER = "foretoken_dt.feature_capture"


@torch.library.custom_op("foretoken_dt::capture_feature", mutates_args=())
def capture_feature(source: torch.Tensor, slot: int) -> torch.Tensor:
    """Return a retained feature copy whose readiness is recorded in the native graph."""
    capture = get_forward_context().no_compile_layers[_CAPTURE_LAYER]
    return capture.copy_feature(source, slot)


@capture_feature.register_fake
def capture_feature_fake(source: torch.Tensor, slot: int) -> torch.Tensor:
    """Expose the copied feature's shape to native graph compilation."""
    return torch.empty_like(source)


class FeatureCapture:
    """Own layer-ready events and transfer-buffer reuse across native target forwards."""

    def __init__(self, config, model, exchange, device, auxiliary):
        self.holder = None
        self.layer_slots = {}
        if auxiliary:
            (self.holder,) = (
                module
                for module in model.modules()
                if isinstance(module, EagleModelMixin)
            )
            self.layer_slots = {
                layer: slot
                for slot, layer in enumerate(self.holder.aux_hidden_state_layers)
            }
        shape = [
            len(self.layer_slots) or 1,
            config.scheduler_config.max_num_batched_tokens,
            config.model_config.get_hidden_size(),
        ]
        self.exchange = exchange
        self.destination = torch.empty(
            shape, dtype=config.model_config.dtype, device=device
        )
        if exchange is not None:
            exchange.allocate(
                "features", shape, str(config.model_config.dtype).removeprefix("torch.")
            )
        self.copy_stream = torch.cuda.Stream(device)
        self.copied = torch.cuda.Event(blocking=True)
        self.ready = [
            torch.cuda.Event(blocking=True, external=True)
            for _ in range(self.destination.shape[0])
        ]
        self.transferred: Future[None] | None = None
        self.send_done: torch.cuda.Event | None = None
        self.config = config
        config.compilation_config.static_forward_context[_CAPTURE_LAYER] = self
        if self.holder is not None:
            self.holder.aux_hidden_state_capture = self.capture_layer
        self.forward_hook = (
            model.register_forward_hook(self.finish_forward)
            if self.holder is None
            else None
        )

    def capture_layer(self, layer, hidden_states):
        """Capture the selected native EAGLE feature without changing its value."""
        slot = self.layer_slots[layer]
        return capture_feature(hidden_states, slot)

    def copy_feature(self, source, slot):
        """Retain a produced layer and record its readiness inside the current native graph."""
        # Stay on the producer stream: a piecewise graph ends before the model's
        # forward hook, so a copy-stream branch could not be joined there.
        destination = self.destination[slot, : len(source)]
        destination.copy_(source)
        self.ready[slot].record(torch.cuda.current_stream(source.device))
        return destination

    def finish_forward(self, model, arguments, output):
        """Capture final hidden states for native methods without auxiliary layers."""
        if self.holder is None:
            return capture_feature(output, 0)

    def transfer(self, rows, destination, channel=None, exchange=None):
        """Publish ready layers while the Target finishes computing and sampling."""
        if channel is not None:
            channel.send_features(self.destination, self.ready, rows)
            self.send_done = channel.input_ready
            return
        exchange = exchange or self.exchange
        if exchange is None:
            raise RuntimeError("feature transfer has no tensor exchange")
        with torch.cuda.device(self.destination.device):
            for layer, ready in enumerate(self.ready):
                ready.synchronize()
                with torch.cuda.stream(self.copy_stream):
                    exchange.buffers["features"][layer, :rows].copy_(
                        self.destination[layer, :rows], non_blocking=True
                    )
                    self.copied.record(self.copy_stream)
                self.copied.synchronize()
                exchange.write_plane("features", layer, rows, destination)

    def prepare_forward(self):
        """Retire the preceding buffer read before another forward overwrites its features."""
        if self.transferred is not None:
            self.transferred.result()
            self.transferred = None
            if self.send_done is not None:
                torch.cuda.current_stream(self.destination.device).wait_event(
                    self.send_done
                )

    def begin_transfer(self):
        """Give one proposal operation ownership of the current forward's feature buffers."""
        self.transferred = Future()
        return self.transferred

    def close(self):
        """Finish device copies before the transport owner unregisters their destinations."""
        for ready in self.ready:
            ready.synchronize()
        self.copy_stream.synchronize()
        if self.forward_hook is not None:
            self.forward_hook.remove()
        if self.holder is not None:
            self.holder.aux_hidden_state_capture = None
        del self.config.compilation_config.static_forward_context[_CAPTURE_LAYER]

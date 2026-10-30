# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Observe recorded external-input events without blocking native scheduling."""

from threading import Condition, Thread

import torch


class ExternalInputObserver:
    """Own event references and coalesced host notifications until Worker teardown."""

    def __init__(self, device):
        self.device = device
        self.condition = Condition()
        self.pending = {}
        self.ready = {}
        self.wakeup = None
        self.notified = False
        self.error = None
        self.stopping = False
        self.thread = None

    def register_wakeup(self, callback):
        """Bind the Core's host-only wake callback before external turns arrive."""
        with self.condition:
            self.wakeup = callback
            notify = self._arm_wakeup()
        if notify is not None:
            notify()

    def register(self, event, identities, owner):
        """Retain an already-recorded event and immutable turn identities for observation."""
        with self.condition:
            if self.stopping:
                raise RuntimeError("External input observation is stopping")
            if self.wakeup is None:
                raise RuntimeError("External input wakeup is not connected")
            for identity in identities:
                if identity in self.pending or identity in self.ready:
                    raise ValueError("External input revision is already registered")
                self.pending[identity] = (event, owner)
            if self.thread is None:
                self.thread = Thread(
                    target=self._observe, name="draft-input-ready", daemon=True
                )
                self.thread.start()
            self.condition.notify()

    def take_ready(self):
        """Drain ready identities on the Core thread and surface observation failures."""
        with self.condition:
            if self.error is not None:
                raise RuntimeError("External input observation failed") from self.error
            identities = list(self.ready)
            self.ready.clear()
            self.notified = False
            return identities

    def forget(self, request_ids):
        """Retire notifications after the native owner has fenced cancelled requests."""
        removed = set(request_ids)
        with self.condition:
            for entries in (self.pending, self.ready):
                for identity in list(entries):
                    if identity[0] in removed:
                        del entries[identity]
            self.condition.notify()

    def close(self):
        """Join event observation before Worker teardown releases its CUDA context."""
        with self.condition:
            self.stopping = True
            self.condition.notify()
        if self.thread is not None:
            self.thread.join()
        with self.condition:
            self.pending.clear()
            self.ready.clear()
            self.wakeup = None

    def _arm_wakeup(self):
        if (
            self.wakeup is not None
            and not self.notified
            and (self.ready or self.error is not None)
        ):
            self.notified = True
            return self.wakeup
        return None

    def _observe(self):
        """Query every pending event so one slow peer cannot block ready peers."""
        try:
            with torch.cuda.device(self.device):
                while True:
                    with self.condition:
                        while not self.pending and not self.stopping:
                            self.condition.wait()
                        if self.stopping:
                            return
                        snapshot = list(self.pending.items())
                    # One peer batch shares an event across its request rows.
                    # Query that event once instead of once per scheduled row.
                    completed_events = {}
                    for _, retained in snapshot:
                        event = retained[0]
                        if id(event) not in completed_events:
                            completed_events[id(event)] = event.query()
                    completed = [
                        (identity, retained)
                        for identity, retained in snapshot
                        if completed_events[id(retained[0])]
                    ]
                    with self.condition:
                        if self.stopping:
                            return
                        for identity, retained in completed:
                            if self.pending.get(identity) is retained:
                                self.ready[identity] = self.pending.pop(identity)
                        notify = self._arm_wakeup()
                    if notify is not None:
                        notify()
                    with self.condition:
                        if self.pending and not self.stopping:
                            # This observer's cadence does not delay Core work;
                            # its latency and CPU cost require GPU measurement.
                            self.condition.wait(timeout=0.0001)
        # Forward every observer failure to Core; an uncaught thread error
        # would otherwise leave accepted native requests asleep indefinitely.
        except Exception as error:  # noqa: BLE001
            with self.condition:
                self.error = error
                notify = self._arm_wakeup()
            if notify is not None:
                notify()

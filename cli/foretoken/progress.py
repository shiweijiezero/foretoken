# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Follow workload logs alongside controller-owned serving readiness."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from typing import Any

from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError, ResourceRef

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Labels identify Foretoken workloads; ownership is resolved by UID, never by name prefix.
_WORKLOAD_LABELS = (
    "inference.foretoken.io/model-group",
    "inference.foretoken.io/frontend-service",
    "inference.foretoken.io/kv-group",
    "inference.foretoken.io/kvservice",
)


def _line(text: str) -> str:
    """Remove terminal controls from a Kubernetes status message before printing it."""
    return " ".join("".join(c for c in _ANSI.sub("", text) if c.isprintable()).split())


class StartupProgress:
    """Own container log followers for one CLI wait or status watch.

    Kubernetes supplies log contents and source prefixes. Service readiness is
    observed separately; leaving this context closes only local kubectl processes.
    """

    def __init__(self, kubectl: Kubectl, emit: Callable[[str], None]) -> None:
        self.kubectl = kubectl
        self.emit = emit
        self._next_poll = 0.0
        self._previous: dict[tuple[str, str, int], dict[str, tuple[str, float]]] = {}
        self._owners: dict[str, dict[str, Any]] = {}
        self._seen_owners: set[str] = set()
        self._logs: dict[tuple[str, str, str], subprocess.Popen] = {}
        self._unavailable = ""

    def __enter__(self) -> StartupProgress:
        """Scope all log readers to the calling deployment wait or status watch."""
        return self

    def __exit__(self, *_: object) -> None:
        """Reap log readers on readiness, failure, timeout, or interruption."""
        self._stop(self._logs.values())
        self._logs.clear()

    @staticmethod
    def _stop(processes: Iterable[subprocess.Popen]) -> None:
        """Close read-only log connections without leaving local child processes."""
        readers = list(processes)
        for process in readers:
            if process.poll() is None:
                process.kill()
        for process in readers:
            process.wait()

    def _follow(
        self, pod: dict[str, Any], container: dict[str, Any],
        live_readers: set[tuple[str, str, str]],
    ) -> str:
        """Attach once per actual container identity and report failed log connections."""
        meta = pod["metadata"]
        previous = container.get("lastState", {}).get("terminated", {})
        sources = [("previous", previous.get("containerID"), False)]
        state = container.get("state", {})
        if "running" in state or "terminated" in state:
            sources.append(("current", container.get("containerID"), "running" in state))
        failures = []
        for source, container_id, follow in sources:
            if not container_id:
                continue
            # A running container becomes lastState after exit. Its runtime ID,
            # not restart-count arithmetic, identifies logs already followed.
            key = (meta["uid"], container["name"], container_id)
            live_readers.add(key)
            if key not in self._logs:
                self._logs[key] = self._log_process(
                    meta, container["name"], follow=follow, previous=source == "previous",
                )
            if (code := self._logs[key].poll()) not in (None, 0):
                failures.append(f"{source} log reader exited with code {code}")
        return "; ".join(failures)

    def _log_process(
        self, metadata: dict[str, Any], container: str, *,
        follow: bool = False, previous: bool = False,
    ) -> subprocess.Popen:
        """Stream all available log lines directly through kubectl's native source prefixes."""
        args = [
            "logs", metadata["name"], "-n", metadata["namespace"], "-c", container,
            "--prefix=true", "--tail=-1",
        ]
        if follow:
            args.append("--follow=true")
        if previous:
            args.append("--previous=true")
        try:
            return subprocess.Popen(self.kubectl.command(args), stdin=subprocess.DEVNULL)
        except OSError as exc:
            raise DeploymentError(
                f"cannot follow logs for {metadata['name']}/{container}: {exc}"
            ) from exc

    def _get(self, args: list[str], deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeploymentError("startup observation time budget exhausted")
        output = self.kubectl.run(
            [*args, "--request-timeout=3s", "-o", "json"],
            timeout=min(remaining, 4.0),
        ).stdout
        return json.loads(output) if output.strip() else {}

    def _service(
        self, obj: dict[str, Any], namespace: str,
        selected: set[tuple[str, str, str]], cache: dict[str, dict[str, Any]],
        deadline: float,
    ) -> ResourceRef | None:
        """Follow controller references to a selected service, checking every UID."""
        visited: set[str] = set()
        while True:
            metadata = obj.get("metadata", {})
            identity = (namespace, obj.get("kind", ""), metadata.get("name", ""))
            if identity in selected:
                return ResourceRef(kind=identity[1], name=identity[2], namespace=namespace)
            owner = next((o for o in metadata.get("ownerReferences", [])
                          if o.get("controller")), None)
            if owner is None or owner["uid"] in visited:
                return None
            uid = owner["uid"]
            visited.add(uid)
            self._seen_owners.add(uid)
            if uid not in cache:
                group = owner["apiVersion"].split("/")[0]
                kind = owner["kind"] if "/" not in owner["apiVersion"] else f"{owner['kind']}.{group}"
                value = self._get(["get", kind, owner["name"], "-n", namespace,
                                   "--ignore-not-found"], deadline)
                if value.get("metadata", {}).get("uid") != uid:
                    return None
                cache[uid] = value
            obj = cache[uid]

    def poll(self, resources: Iterable[ResourceRef], elapsed: float, budget: float = 5.0) -> None:
        """Discover container attempts and report Pod changes without deciding service readiness."""
        now = time.monotonic()
        if now < self._next_poll or budget <= 0:
            return
        self._next_poll = now + 5.0
        deadline = now + min(budget, 5.0)
        selected = {(r.namespace, r.kind, r.name) for r in resources}
        owners = self._owners
        self._seen_owners.clear()
        alive: set[tuple[str, str, int]] = set()
        live_readers: set[tuple[str, str, str]] = set()
        events: dict[str, dict[str, Any]] = {}
        try:
            for namespace in sorted({ns for ns, _, _ in selected}):
                args = ["get", "pods"]
                if namespace:
                    args += ["-n", namespace]
                for pod in self._get(args, deadline).get("items", []):
                    meta = pod["metadata"]
                    if not any(key in meta.get("labels", {}) for key in _WORKLOAD_LABELS):
                        continue
                    service = self._service(pod, namespace, selected, owners, deadline)
                    if service is None:
                        continue
                    status = pod.get("status", {})
                    containers = status.get("initContainerStatuses", []) + status.get("containerStatuses", [])
                    if not containers:
                        scheduled = next((c for c in status.get("conditions", [])
                                          if c.get("type") == "PodScheduled"), {})
                        self._report(pod, {"name": "pod"}, service, elapsed,
                                     scheduled.get("reason", status.get("phase", "Pending")),
                                     scheduled.get("message", "Waiting for container status"), alive)
                    for container in containers:
                        state = container.get("state", {})
                        last = container.get("lastState", {}).get("terminated", {})
                        previous_exit = f"previous exit={last.get('exitCode')} {last.get('reason', '')}" if last else ""
                        if meta.get("deletionTimestamp"):
                            self._report(pod, container, service, elapsed, "Terminating", "", alive)
                        elif "waiting" in state:
                            waiting = state["waiting"]
                            self._report(pod, container, service, elapsed,
                                         waiting.get("reason", "Waiting"), waiting.get("message", ""), alive)
                            if namespace not in events:
                                event_args = ["get", "events"]
                                if namespace:
                                    event_args += ["-n", namespace]
                                events[namespace] = self._get(event_args, deadline)
                            related = [e for e in events[namespace].get("items", [])
                                       if e.get("involvedObject", {}).get("uid") == meta["uid"]]
                            if related:
                                event = max(related, key=lambda e: str(e.get("lastTimestamp") or
                                            e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp", "")))
                                self._report(pod, container, service, elapsed, "Event",
                                             f"{event.get('reason', '')}: {event.get('message', '')}", alive)
                        elif "terminated" in state:
                            end = state["terminated"]
                            self._report(pod, container, service, elapsed,
                                         end.get("reason", "Terminated"),
                                         f"exit={end.get('exitCode')} {end.get('message', '')}", alive)
                        elif container.get("ready"):
                            self._report(pod, container, service, elapsed, "ContainerReady", previous_exit, alive)
                        else:
                            self._report(pod, container, service, elapsed, "Running", previous_exit, alive)
                        if "running" in state or "terminated" in state or container.get("lastState", {}).get("terminated"):
                            failure = self._follow(pod, container, live_readers)
                            if failure:
                                self._report(pod, container, service, elapsed,
                                             "LogsUnavailable", failure, alive)
            # Prune readers only after a complete discovery pass. A transient API
            # failure must not cancel log streams from still-running containers.
            self._stop([self._logs.pop(key) for key in self._logs.keys() - live_readers])
            self._previous = {key: value for key, value in self._previous.items() if key in alive}
            self._owners = {uid: value for uid, value in owners.items() if uid in self._seen_owners}
            if self._unavailable:
                self.emit(f"[{elapsed:6.1f}s] Startup observations resumed")
                self._unavailable = ""
        except DeploymentError as exc:
            # Observability permissions or a disappearing Pod must not redefine service readiness.
            message = _line(str(exc))
            if message != self._unavailable:
                self.emit(f"[{elapsed:6.1f}s] Startup observations unavailable — {message}")
                self._unavailable = message

    def _report(
        self, pod: dict[str, Any], container: dict[str, Any], service: ResourceRef,
        elapsed: float, stage: str, detail: str, alive: set[tuple[str, str, int]],
    ) -> None:
        """Emit changed Pod status and event observations with their container identity."""
        meta = pod["metadata"]
        attempt = int(container.get("restartCount", 0))
        key = (meta["uid"], container["name"], attempt)
        alive.add(key)
        node = pod.get("spec", {}).get("nodeName", "unassigned")
        text = f"{service.display_name} / {meta['name']}/{container['name']} node={node} restart={attempt} {stage} {_line(detail)}".rstrip()
        observations = self._previous.setdefault(key, {})
        channel = "logs" if stage == "LogsUnavailable" else "event" if stage == "Event" else "pod"
        previous = observations.get(channel)
        if previous is None or previous[0] != text or (channel != "logs" and time.monotonic() - previous[1] >= 30):
            self.emit(f"[{elapsed:6.1f}s] {text}")
            observations[channel] = (text, time.monotonic())

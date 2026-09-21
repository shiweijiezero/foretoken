# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read-only startup observations alongside controller-owned serving readiness."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError, ResourceRef

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_NATIVE_PROGRESS = re.compile(
    r"Loading (?:safetensors|checkpoint|weights)|Capturing .*graphs|"
    r"Graph capturing finished|Model loading took|Loading weights took|"
    r"torch\.compile|compilation took|Compiling|Available KV cache memory|"
    r"GPU KV cache size|Initializing .*engine|Starting to load model",
    re.IGNORECASE,
)
_ERROR = re.compile(r"\bERROR\b|\b\w*Error:|Traceback \(most recent call last\)")
# Labels identify Foretoken workloads; ownership is resolved by UID, never by name prefix.
_WORKLOAD_LABELS = (
    "inference.foretoken.io/model-group",
    "inference.foretoken.io/frontend-service",
    "inference.foretoken.io/kv-group",
    "inference.foretoken.io/kvservice",
)


def _line(text: str) -> str:
    """Remove terminal controls from one observed message before printing it."""
    return " ".join("".join(c for c in _ANSI.sub("", text) if c.isprintable()).split())


def _native_messages(text: str) -> tuple[str, ...]:
    """Keep each worker's latest native progress, or its errors, without inferring Ready."""
    progress: dict[str, str] = {}
    errors: list[str] = []
    for raw in text.splitlines():
        line = _line(raw)
        if _ERROR.search(line):
            errors.append(line)
        elif _NATIVE_PROGRESS.search(line):
            worker = re.search(r"\((?:Worker|EngineCore)[^)]*\)", line)
            progress[worker.group(0) if worker else "engine"] = line
    causes = [line for line in errors if re.search(r"\b[A-Z]\w*Error:\s", line)]
    return tuple(causes[-3:] if causes else errors[-3:] if errors else progress.values())


class StartupProgress:
    """Observe selected workloads during CLI waits; never mutate resources or readiness.

    Polls are bounded and log reads rotate across containers. Pod UID and restart
    count separate attempts so replaced workers cannot inherit a completed bar.
    """

    def __init__(self, kubectl: Kubectl, emit: Callable[[str], None]) -> None:
        self.kubectl = kubectl
        self.emit = emit
        self._next_poll = 0.0
        self._previous: dict[tuple[str, str, int], dict[str, tuple[str, float]]] = {}
        self._owners: dict[str, dict[str, Any]] = {}
        self._seen_owners: set[str] = set()
        self._cursor = 0
        self._unavailable = ""

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
        """Print changed Pod stages and native progress within the waiter's remaining budget."""
        now = time.monotonic()
        if now < self._next_poll or budget <= 0:
            return
        self._next_poll = now + 5.0
        deadline = now + min(budget, 5.0)
        selected = {(r.namespace, r.kind, r.name) for r in resources}
        owners = self._owners
        self._seen_owners.clear()
        logs: list[tuple[dict[str, Any], dict[str, Any], ResourceRef]] = []
        alive: set[tuple[str, str, int]] = set()
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
                        elif container.get("ready") and container["name"] != "model-server":
                            self._report(pod, container, service, elapsed, "ContainerReady", "", alive)
                        else:
                            last = container.get("lastState", {}).get("terminated", {})
                            self._report(pod, container, service, elapsed, "Running",
                                         f"previous exit={last.get('exitCode')} {last.get('reason', '')}" if last else "", alive)
                            logs.append((pod, container, service))
            self._previous = {key: value for key, value in self._previous.items() if key in alive}
            self._owners = {uid: value for uid, value in owners.items() if uid in self._seen_owners}
            if logs and time.monotonic() < deadline:
                pod, container, service = logs[self._cursor % len(logs)]
                self._cursor += 1
                ns = pod["metadata"]["namespace"]
                text = self.kubectl.run(
                    ["logs", pod["metadata"]["name"], "-n", ns, "-c", container["name"],
                     "--tail=200", "--limit-bytes=65536", "--request-timeout=3s"],
                    timeout=max(0.01, min(4.0, deadline - time.monotonic())),
                ).stdout
                current = self._get(["get", "pod", pod["metadata"]["name"], "-n", ns,
                                     "--ignore-not-found"], deadline)
                current_status = current.get("status", {})
                current_containers = (current_status.get("initContainerStatuses", [])
                                      + current_status.get("containerStatuses", []))
                current_container = next((c for c in current_containers
                                          if c["name"] == container["name"]), {})
                if (current.get("metadata", {}).get("uid") == pod["metadata"]["uid"]
                        and current_container.get("restartCount") == container.get("restartCount")):
                    for message in _native_messages(text):
                        self._report(pod, container, service, elapsed, "Engine", message, alive)
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
        """Emit changed observations, including a periodic heartbeat for unchanged stages."""
        meta = pod["metadata"]
        attempt = int(container.get("restartCount", 0))
        key = (meta["uid"], container["name"], attempt)
        alive.add(key)
        node = pod.get("spec", {}).get("nodeName", "unassigned")
        text = f"{service.display_name} / {meta['name']}/{container['name']} node={node} restart={attempt} {stage} {_line(detail)}".rstrip()
        observations = self._previous.setdefault(key, {})
        worker = re.search(r"\((?:Worker|EngineCore)[^)]*\)", detail)
        channel = (worker.group(0) if worker else "engine") if stage == "Engine" else "event" if stage == "Event" else "pod"
        previous = observations.get(channel)
        heartbeat = channel != "pod" or not any(k != "pod" for k in observations)
        if previous is None or previous[0] != text or (heartbeat and time.monotonic() - previous[1] >= 30):
            self.emit(f"[{elapsed:6.1f}s] {text}")
            observations[channel] = (text, time.monotonic())

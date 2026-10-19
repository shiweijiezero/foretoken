# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run Kubernetes commands and observe generic resource operations."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from foretoken.command_execution import command_deadline, run_command
from foretoken.errors import DeploymentError
from foretoken.kubernetes.namespace import mutation_lock_fds
from foretoken.tools import resolve_tool


@dataclass(frozen=True)
class ResourceRef:
    """A Kubernetes resource identity used by command and lifecycle callers."""

    kind: str
    name: str
    namespace: str

    @property
    def display_name(self) -> str:
        """Return the user-facing kind and resource name."""
        return f"{self.kind}/{self.name}"


class KubernetesServiceUnavailableError(DeploymentError):
    """Identify API-server unavailability for callers that own a readiness deadline."""


class KubernetesVersionConflictError(DeploymentError):
    """Identify failed API version preconditions without reclassifying field ownership errors."""


class Kubectl:
    """Run kubectl in the selected context, optionally sharing one monotonic operation deadline."""

    def __init__(self, context: str | None = None, *, deadline: float | None = None) -> None:
        self.context = context
        self.deadline = deadline
        self.executable = resolve_tool("kubectl", "FORETOKEN_KUBECTL")

    @contextmanager
    def until(self, deadline: float) -> Iterator[float]:
        """Share the observation deadline while retaining this client's configured bound."""
        effective = min(self.deadline, deadline) if self.deadline is not None else deadline
        with command_deadline(effective) as active_deadline:
            yield active_deadline

    def _timeout(self, timeout: float | None) -> float | None:
        """Apply the client's configured monotonic deadline to a command."""
        if self.deadline is None:
            return timeout
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DeploymentError("Kubernetes operation deadline exceeded")
        return remaining if timeout is None else min(timeout, remaining)

    def command(self, args: Iterable[str]) -> list[str]:
        """Build an invocation with the same cluster selection for text and streamed calls."""
        context = ["--context", self.context] if self.context is not None else []
        return [self.executable, *context, *args]

    def run(
        self,
        args: Iterable[str],
        *,
        input_text: str | None = None,
        timeout: float | None = None,
        output: BinaryIO | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run kubectl within its deadline; callers may handle completed command failures."""
        command = self.command(args)
        timeout = self._timeout(timeout)
        completed = run_command(
            command,
            input=input_text,
            text=True,
            stdout=output if output is not None else subprocess.PIPE,
            pass_fds=mutation_lock_fds.get(),
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if completed.returncode and check:
            detail = (
                completed.stderr.strip() or (completed.stdout or "").strip()
                or f"exit status {completed.returncode}"
            )
            message = f"{' '.join(command)} failed: {detail}"
            if detail.startswith("Error from server (ServiceUnavailable):"):
                raise KubernetesServiceUnavailableError(message)
            if (
                "the object has been modified; please apply your changes to the latest version" in detail
                or "metadata.resourceVersion: Invalid value: 0: must be specified for an update" in detail
            ):
                raise KubernetesVersionConflictError(message)
            raise DeploymentError(message)
        return completed

    def get_raw(self, path: str, request_timeout: str) -> str:
        """Return a bounded Kubernetes API or resource-proxy response as text."""
        return self.run(
            ["get", f"--request-timeout={request_timeout}", "--raw", path],
            timeout=timeout_seconds(request_timeout) or None,
        ).stdout

    def download_raw(self, path: str, destination: Path, request_timeout: str) -> None:
        """Save a binary API response atomically without decoding it or exposing partial output."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                self.run(
                    ["get", f"--request-timeout={request_timeout}", "--raw", path],
                    timeout=timeout_seconds(request_timeout) or None,
                    output=output,
                )
                output.close()
                temporary.replace(destination)
            finally:
                output.close()
                temporary.unlink(missing_ok=True)

    def get_raw_if_not_found(self, path: str, request_timeout: str) -> str | None:
        """Read a raw endpoint and return None only for the API server's NotFound response."""
        command = self.command(
            ["get", f"--request-timeout={request_timeout}", "--raw", path]
        )
        timeout = self._timeout(timeout_seconds(request_timeout))
        completed = run_command(
            command, text=True, capture_output=True, timeout=timeout,
        )
        if completed.returncode == 0:
            return completed.stdout
        detail = completed.stderr.strip() or completed.stdout.strip()
        if detail.startswith("Error from server (NotFound):"):
            return None
        raise DeploymentError(f"{' '.join(command)} failed: {detail}")

    def current_namespace(self) -> str:
        """Resolve the selected context's namespace for namespace-local deployment ownership."""
        return self.run([
            "config", "view", "--minify", "-o", "jsonpath={.contexts[0].context.namespace}",
        ]).stdout.strip() or "default"

    def kustomize(self, path: Path) -> str:
        """Render a Kustomize root through the installed kubectl."""
        return self.run(["kustomize", str(path)]).stdout

    def apply(self, rendered: str) -> None:
        """Server-side apply one rendered payload and leave resources user-owned."""
        self.run(
            ["apply", "--server-side", "--filename=-"],
            input_text=rendered,
        )

    def delete(self, rendered: str, timeout: str) -> None:
        """Delete one rendered payload and wait for its resources to terminate."""
        self.run(
            [
                "delete",
                "--filename=-",
                "--ignore-not-found",
                "--wait=true",
                f"--timeout={timeout}",
            ],
            input_text=rendered,
        )

    def delete_object(self, value: dict[str, Any], timeout: str, *, wait: bool = True) -> None:
        """Conditionally delete one observed UID/version; callers may wait outside a mutation lock."""
        metadata = value["metadata"]
        version = value["apiVersion"]
        base = f"/api/{version}" if "/" not in version else f"/apis/{version}"
        resources = json.loads(self.get_raw(base, timeout))["resources"]
        resource = next(item for item in resources if item["kind"] == value["kind"] and "/" not in item["name"])
        path = base
        if resource["namespaced"]:
            path += "/namespaces/" + metadata["namespace"]
        path += "/" + resource["name"] + "/" + metadata["name"]
        self.run(
            ["delete", "--raw", path, "--filename=-"],
            input_text=json.dumps({
                "apiVersion": "v1", "kind": "DeleteOptions",
                "preconditions": {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]},
                "propagationPolicy": "Foreground",
            }),
            timeout=timeout_seconds(timeout),
        )
        if wait:
            self.wait_deleted(value["kind"], metadata, timeout)

    def wait_deleted(
        self, resource: str, metadata: dict[str, Any], timeout: str
    ) -> None:
        """Wait for one owned UID to disappear without waiting on a reused resource name."""
        name, namespace, uid = metadata["name"], metadata.get("namespace", ""), metadata["uid"]
        deadline = time.monotonic() + timeout_seconds(timeout)
        while True:
            current = self.get_if_exists(
                resource, name, namespace, timeout=max(1, deadline - time.monotonic())
            )
            if current is None or current["metadata"]["uid"] != uid:
                return
            if time.monotonic() >= deadline:
                raise DeploymentError(
                    f"{resource}/{name} in {namespace} was not deleted within {timeout}"
                )
            time.sleep(1)

    def rollout_status(self, resource: ResourceRef, timeout: str) -> None:
        """Wait for one namespaced workload to become ready."""
        self.run(
            [
                "rollout",
                "status",
                f"{resource.kind.lower()}/{resource.name}",
                "--namespace",
                resource.namespace,
                f"--timeout={timeout}",
            ],
            timeout=timeout_seconds(timeout),
        )

    def wait_for_crds(self, names: tuple[str, ...], timeout: str) -> None:
        """Wait for installed CRDs before applying their custom resources."""
        self.run(
            [
                "wait",
                "--for=condition=Established",
                *(f"crd/{name}" for name in names),
                f"--timeout={timeout}",
            ]
        )

    def label_namespace(self, name: str, key: str, value: str) -> None:
        """Set one namespace label required by a platform integration."""
        self.run(
            [
                "label",
                "namespace",
                name,
                f"{key}={value}",
                "--overwrite",
            ]
        )

    def annotate_namespace(self, name: str, key: str, value: str) -> None:
        """Set one namespace annotation that records integration ownership."""
        self.run(
            [
                "annotate",
                "namespace",
                name,
                f"{key}={value}",
                "--overwrite",
            ]
        )

    def remove_namespace_label(self, name: str, key: str) -> None:
        """Remove one namespace label owned by the CLI."""
        self.run(["label", "namespace", name, f"{key}-"])

    def remove_namespace_annotation(self, name: str, key: str) -> None:
        """Remove one namespace annotation owned by the CLI."""
        self.run(["annotate", "namespace", name, f"{key}-"])

    def restart_deployment(
        self, name: str, namespace: str, timeout: str
    ) -> None:
        """Restart one Deployment and wait for its rollout to complete."""
        self.run(
            [
                "rollout",
                "restart",
                f"deployment/{name}",
                "--namespace",
                namespace,
            ]
        )
        self.run(
            [
                "rollout",
                "status",
                f"deployment/{name}",
                "--namespace",
                namespace,
                f"--timeout={timeout}",
            ]
        )

    def exists(self, kind: str, name: str, namespace: str = "") -> bool:
        """Return whether a named Kubernetes resource currently exists."""
        args = ["get", kind, name, "--ignore-not-found", "-o", "name"]
        if namespace:
            args.extend(["--namespace", namespace])
        return bool(self.run(args).stdout.strip())

    def get(
        self,
        kind: str,
        name: str,
        namespace: str = "",
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Return one Kubernetes object as decoded JSON within an optional timeout."""
        args = ["get", kind, name]
        if namespace:
            args.extend(["--namespace", namespace])
        return _decode_object(
            self.run([*args, "-o", "json"], timeout=timeout).stdout
        )

    def get_if_exists(
        self, kind: str, name: str, namespace: str = "", *, timeout: float | None = None
    ) -> dict[str, Any] | None:
        """Return one Kubernetes object, or None while it does not exist."""
        args = ["get", kind, name, "--ignore-not-found"]
        if namespace:
            args.extend(["--namespace", namespace])
        output = self.run([*args, "-o", "json"], timeout=timeout).stdout.strip()
        return _decode_object(output) if output else None

    def get_resources(
        self,
        resources: tuple[ResourceRef, ...],
        *,
        timeout: float | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Return named resources from one namespace within an optional timeout."""
        namespaces = {resource.namespace for resource in resources}
        if len(namespaces) != 1:
            raise DeploymentError("selected resources must share one namespace")
        if len(resources) == 1:
            resource = resources[0]
            return (
                self.get(
                    resource.kind,
                    resource.name,
                    resource.namespace,
                    timeout=timeout,
                ),
            )
        args = [
            "get",
            *(f"{resource.kind.lower()}/{resource.name}" for resource in resources),
        ]
        namespace = next(iter(namespaces))
        if namespace:
            args.extend(["--namespace", namespace])
        return _decode_resource_list(
            self.run([*args, "-o", "json"], timeout=timeout).stdout
        )

    def list_resources(
        self, kinds: Iterable[str], namespace: str, *, label_selector: str = ""
    ) -> tuple[dict[str, Any], ...]:
        """Return selected namespaced resources, optionally filtered by labels."""
        args = [
            "get",
            ",".join(kinds),
            "--namespace",
            namespace,
            "-o",
            "json",
        ]
        if label_selector:
            args.extend(["--selector", label_selector])
        return _decode_resource_list(self.run(args).stdout)

    def list_cluster_resources(
        self, kinds: Iterable[str], *, label_selector: str = ""
    ) -> tuple[dict[str, Any], ...]:
        """Return selected cluster-scoped resource kinds."""
        args = ["get", ",".join(kinds), "-o", "json"]
        if label_selector:
            args.extend(["--selector", label_selector])
        return _decode_resource_list(self.run(args).stdout)

    def list_all_resources(
        self, kinds: Iterable[str], *, label_selector: str = ""
    ) -> tuple[dict[str, Any], ...]:
        """Return selected resource kinds across every namespace."""
        args = ["get", ",".join(kinds), "--all-namespaces", "-o", "json"]
        if label_selector:
            args.extend(["--selector", label_selector])
        return _decode_resource_list(self.run(args).stdout)

    def api_resource_names(self, group: str | None = None) -> tuple[str, ...]:
        """Discover served resources, optionally selecting one API group; empty selects core."""
        args = ["api-resources", "-o", "name"]
        if group is not None:
            args.extend(["--api-group", group])
        output = self.run(args).stdout
        return tuple(line.strip() for line in output.splitlines() if line.strip())

    def current_context(self) -> str:
        """Return the active kubeconfig context used by deployment operations."""
        return self.run(["config", "current-context"]).stdout.strip()


def _decode_object(output: str) -> dict[str, Any]:
    """Decode the Kubernetes object contract returned by kubectl JSON output."""
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentError("kubectl returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DeploymentError("kubectl returned an unexpected JSON value")
    return value


def _decode_resource_list(output: str) -> tuple[dict[str, Any], ...]:
    """Decode a Kubernetes List and return its object items."""
    items = _decode_object(output).get("items") or []
    if not isinstance(items, list) or not all(
        isinstance(item, dict) for item in items
    ):
        raise DeploymentError("kubectl returned an unexpected resource list")
    return tuple(items)


def resource_ref(value: dict[str, Any]) -> ResourceRef:
    """Return one Kubernetes identity and reject an incomplete object contract."""
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        raise DeploymentError("Kubernetes object has no metadata")
    kind = value.get("kind")
    name = metadata.get("name")
    if not isinstance(kind, str) or not kind:
        raise DeploymentError("Kubernetes object has no kind")
    if not isinstance(name, str) or not name:
        raise DeploymentError(f"Kubernetes {kind} object has no metadata.name")
    namespace_value = metadata.get("namespace")
    if namespace_value is not None and not isinstance(namespace_value, str):
        raise DeploymentError(
            f"Kubernetes {kind}/{name} has an invalid metadata.namespace"
        )
    namespace = "" if namespace_value is None else namespace_value
    return ResourceRef(kind, name, namespace)


def timeout_seconds(value: str) -> float:
    """Convert a Kubernetes-style duration used by deployment waits."""
    parts = re.findall(r"(\d+)([smh])", value)
    if not parts or "".join(f"{amount}{unit}" for amount, unit in parts) != value:
        raise DeploymentError(
            "--timeout must use Kubernetes duration syntax, such as 10m or 1h30m"
        )
    multipliers = {"s": 1, "m": 60, "h": 3600}
    return float(sum(int(amount) * multipliers[unit] for amount, unit in parts))


def wait_for_object(
    fetch: Callable[[], dict[str, Any]],
    ready: Callable[[dict[str, Any]], bool],
    deadline: float,
    description: str,
) -> dict[str, Any]:
    """Poll a Kubernetes dependency until its readiness callback succeeds or the deadline passes."""
    while time.monotonic() < deadline:
        value = fetch()
        if ready(value):
            return value
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    raise DeploymentError(f"timed out waiting for {description}")

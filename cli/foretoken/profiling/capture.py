# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Submit and observe service-owned captures through the Kubernetes API."""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Literal

from foretoken.arguments import ProfileCommand
from foretoken.kubernetes import Kubectl, load_deployment, timeout_seconds
from foretoken.manifest import DeploymentError, ForetokenDeployment


class ProfileRun:
    """CLI client for one retained capture; the controller owns native execution and storage."""

    def __init__(
        self, command: ProfileCommand, *, deployment: ForetokenDeployment | None = None
    ) -> None:
        self.command = command
        self.wait_seconds = timeout_seconds(command.timeout)
        if self.wait_seconds <= 0:
            raise DeploymentError("capture wait timeout must be positive")
        self.kubectl = Kubectl()
        if deployment is None:
            deployment = load_deployment(command.kustomize_path, self.kubectl)
        selected = [
            name for name, model in deployment.models.items()
            if command.model is None or model == command.model
        ]
        if len(selected) != 1:
            raise DeploymentError(
                "profile requires one ModelService; select a unique model with --model"
            )
        self.namespace = deployment.namespace
        self.service_name = selected[0]
        self.name = ""
        self.uid = ""
        self.status: dict[str, Any] = {}
        self._previous: tuple[str, str] | None = None

    @property
    def terminal(self) -> bool:
        """Report whether the last observation confirms the capture has ended."""
        return self.status.get("phase") in {"Succeeded", "Failed", "Cancelled"}

    def start(self) -> None:
        """Create one capture and retain its identity for observation and cancellation."""
        resource = {
            "apiVersion": "inference.foretoken.io/v1alpha1",
            "kind": "ProfileRun",
            "metadata": {"generateName": "profile-", "namespace": self.namespace},
            "spec": {
                "modelServiceRef": {"name": self.service_name},
                "engine": self.command.profile_engine,
                "duration": self.command.profile_duration,
                "action": "Capture",
            },
        }
        created = json.loads(self.kubectl.run(
            ["create", "-f", "-", "-o", "json", "--request-timeout=20s"],
            input_text=json.dumps(resource),
        ).stdout)
        self.name, self.uid = created["metadata"]["name"], created["metadata"]["uid"]
        print(f"ProfileRun {self.namespace}/{self.name}", flush=True)
        print("View captures: foretoken profile view", flush=True)

    def observe(self) -> dict[str, Any]:
        """Read this capture's status and print progress changes."""
        run = json.loads(self.kubectl.run([
            "get", "profilerun", self.name, "-n", self.namespace,
            "-o", "json", "--request-timeout=20s",
        ]).stdout)
        if run["metadata"]["uid"] != self.uid:
            raise DeploymentError("ProfileRun was replaced; refusing to observe a different run")
        self.status = run.get("status", {})
        progress = (self.status.get("phase", "Pending"), self.status.get("message", ""))
        if progress != self._previous:
            print(f"{progress[0]}: {progress[1]}".rstrip(": "), flush=True)
            self._previous = progress
        return self.status

    def request(self, action: Literal["Finish", "Cancel"]) -> None:
        """Stop only this capture; Finish cannot override another caller's cancellation."""
        if not self.uid or self.terminal:
            return
        patch = [{"op": "test", "path": "/metadata/uid", "value": self.uid}]
        if action == "Finish":
            patch.append({"op": "test", "path": "/spec/action", "value": "Capture"})
        patch.append({"op": "replace", "path": "/spec/action", "value": action})
        self.kubectl.run([
            "patch", "profilerun", self.name, "-n", self.namespace,
            "--type=json", "-p", json.dumps(patch), "--request-timeout=20s",
        ])
        print(f"{action} requested for ProfileRun {self.namespace}/{self.name}.", flush=True)

    def cancel(self) -> None:
        """Request cancellation during caller cleanup without hiding the original failure."""
        try:
            self.request("Cancel")
        except DeploymentError as error:
            print(
                f"Could not submit cancellation: {error}. Runtime deadlines remain active.",
                file=sys.stderr,
            )

    def wait(self, *, require_success: bool = True) -> None:
        """Wait for completion; cleanup callers may accept cancelled or failed captures."""
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            self.observe()
            if self.terminal:
                if require_success and self.status["phase"] != "Succeeded":
                    raise DeploymentError(
                        f"capture {self.status['phase'].lower()}: {self.status.get('message', '')}"
                    )
                return
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        raise DeploymentError(
            f"stopped waiting after {self.command.timeout}; ProfileRun {self.namespace}/{self.name} "
            "continues independently. Inspect its status with the command above."
        )

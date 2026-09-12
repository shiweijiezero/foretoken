# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Submit and observe one service-owned capture through the Kubernetes API."""

from __future__ import annotations

import json
import sys
import time

from foretoken.arguments import ProfileCommand
from foretoken.kubernetes import Kubectl, load_deployment, timeout_seconds
from foretoken.manifest import DeploymentError


def capture(command: ProfileCommand) -> None:
    """Create a retained ProfileRun; the controller owns execution and stop after CLI exit."""
    wait_seconds = timeout_seconds(command.timeout)
    if wait_seconds <= 0:
        raise DeploymentError("--timeout must be positive")
    kubectl = Kubectl()
    deployment = load_deployment(command.kustomize_path, kubectl)
    selected = [
        name
        for name, model in deployment.models.items()
        if command.model is None or model == command.model
    ]
    if len(selected) != 1:
        raise DeploymentError(
            "profile requires one ModelService; select a unique model with --model"
        )
    namespace = deployment.namespace
    spec: dict[str, object] = {
        "modelServiceRef": {"name": selected[0]},
        "engine": command.profile_engine,
        "duration": command.profile_duration,
        "action": "Capture",
    }
    resource = {
        "apiVersion": "inference.foretoken.io/v1alpha1",
        "kind": "ProfileRun",
        "metadata": {"generateName": "profile-", "namespace": namespace},
        "spec": spec,
    }
    created = json.loads(
        kubectl.run(
            ["create", "-f", "-", "-o", "json", "--request-timeout=20s"],
            input_text=json.dumps(resource),
        ).stdout
    )
    name, uid = created["metadata"]["name"], created["metadata"]["uid"]
    print(f"ProfileRun {namespace}/{name}", flush=True)
    print(
        f"Inspect later: kubectl get profilerun {name} -n {namespace} -o yaml",
        flush=True,
    )
    deadline = time.monotonic() + wait_seconds
    previous = None
    try:
        while time.monotonic() < deadline:
            run = json.loads(
                kubectl.run(
                    [
                        "get",
                        "profilerun",
                        name,
                        "-n",
                        namespace,
                        "-o",
                        "json",
                        "--request-timeout=20s",
                    ]
                ).stdout
            )
            if run["metadata"]["uid"] != uid:
                raise DeploymentError(
                    "ProfileRun was replaced; refusing to observe a different run"
                )
            status = run.get("status", {})
            phase = status.get("phase", "Pending")
            progress = (phase, status.get("message", ""))
            if progress != previous:
                print(f"{phase}: {progress[1]}".rstrip(": "), flush=True)
                previous = progress
            if phase in {"Succeeded", "Failed", "Cancelled"}:
                artifact = status.get("artifact")
                if artifact:
                    print(
                        f"Artifacts: PVC {namespace}/{artifact['claimName']} "
                        f"— {artifact['path']}",
                        flush=True,
                    )
                if phase != "Succeeded":
                    raise DeploymentError(
                        f"capture {phase.lower()}: {status.get('message', '')}"
                    )
                return
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        # The UID precondition prevents cancelling a replacement resource with the same name.
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "replace", "path": "/spec/action", "value": "Cancel"},
        ]
        try:
            kubectl.run(
                [
                    "patch",
                    "profilerun",
                    name,
                    "-n",
                    namespace,
                    "--type=json",
                    "-p",
                    json.dumps(patch),
                    "--request-timeout=20s",
                ]
            )
            print(
                "Cancellation requested; the runtime will stop and retain its output.",
                file=sys.stderr,
            )
        except DeploymentError as error:
            print(
                f"Could not submit cancellation: {error}. Runtime deadlines remain active.",
                file=sys.stderr,
            )
        raise
    raise DeploymentError(
        f"stopped waiting after {command.timeout}; ProfileRun {namespace}/{name} "
        "remains controller-owned. Inspect its status with the command above."
    )

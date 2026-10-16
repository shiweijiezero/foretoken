# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Stop service execution without rewriting deployment configuration or releasing storage."""

from __future__ import annotations

import copy

import yaml

from foretoken.arguments import StopCommand
from foretoken.kubernetes import (
    Kubectl,
    ResourceProgress,
    load_deployment,
    wait_for_resources,
)
from foretoken.manifest import ResourceRef
from foretoken.submission import DeploymentSubmission


def stop_deployment(command: StopCommand) -> None:
    """Submit suspension before startup locks and wait for controllers to retire every selected service."""
    kubectl = Kubectl()
    deployment = load_deployment(command.kustomize_path, kubectl)
    submission = DeploymentSubmission(kubectl, deployment, command.timeout)
    objects = []
    if not kubectl.exists("namespace", deployment.namespace):
        objects.append({
            "apiVersion": "v1", "kind": "Namespace", "metadata": {"name": deployment.namespace},
        })
    for value in deployment.objects:
        if value.get("kind") not in {"ModelService", "FrontendService"} or not value.get("apiVersion", "").startswith("inference.foretoken.io/"):
            continue
        stopped = copy.deepcopy(value)
        stopped["spec"]["suspend"] = True
        objects.append(stopped)
    submission.apply(yaml.safe_dump_all(objects, sort_keys=False), patch_services=True)

    def report(elapsed: float, progress: ResourceProgress) -> None:
        print(f"[{elapsed:6.1f}s] {progress.resource.display_name}: {progress.state} — {progress.detail or progress.reason}", flush=True)

    def observe(resources: tuple[ResourceRef, ...], elapsed: float, budget: float) -> None:
        submission.check()

    wait_for_resources(deployment.service_refs(), kubectl, command.timeout, report=report, observe=observe)
    submission.check()
    print("Foretoken services stopped; configuration, storage and build caches are retained")

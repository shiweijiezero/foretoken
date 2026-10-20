# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Execute service deployment commands with their subprocess and capture lifetimes."""

from __future__ import annotations

import copy
import time

import yaml

from foretoken.arguments import DeleteCommand, DeployCommand, StopCommand
from foretoken.command_execution import CommandExecution
from foretoken.deployment.instances import resolve_deployment_target
from foretoken.deployment.lifecycle import DeploymentLifecycle
from foretoken.deployment.manifest import load_deployment
from foretoken.deployment.status import ResourceProgress, wait_for_resources
from foretoken.deployment.submission import DeploymentSubmission
from foretoken.kubernetes.client import Kubectl, ResourceRef, timeout_seconds
from foretoken.profiling import ProfileRun


def deploy(command: DeployCommand) -> None:
    """Apply CLI service intent, then own an optional external-traffic capture until completion."""
    with CommandExecution(timeout_seconds(command.timeout)):
        kubectl = Kubectl()
        deployment = load_deployment(command.kustomize_path, kubectl, name=command.name)
        # Resolve profiling intent before changing the deployment.
        capture = (
            ProfileRun(command.profile, deployment=deployment, kubectl=kubectl)
            if command.profile is not None else None
        )
        DeploymentLifecycle(kubectl).apply(
            deployment, command.timeout,
            editable=command.editable, engine_sources=command.engine_sources,
        )
        if capture is not None:
            try:
                capture.start()
                capture.wait()
            except KeyboardInterrupt:
                capture.cancel()
                raise


def delete(command: DeleteCommand) -> None:
    """Delete the CLI-selected deployment and report completion after resource termination."""
    with CommandExecution(timeout_seconds(command.timeout)):
        kubectl = Kubectl()
        deployment = resolve_deployment_target(
            command.target, kubectl, name=command.name, allow_missing=True, missing_instance_ok=True,
        )
        print(f"Deleting deployment instance {deployment.name}")
        DeploymentLifecycle(kubectl).delete(deployment, command.timeout)
        print("Foretoken deployment deleted")


def stop(command: StopCommand) -> None:
    """Submit CLI suspension before build locks and wait for selected services to retire execution."""
    with CommandExecution(timeout_seconds(command.timeout)):
        kubectl = Kubectl()
        deployment = resolve_deployment_target(command.target, kubectl, name=command.name, allow_missing=True)
        submission = DeploymentSubmission(kubectl, deployment, command.timeout)
        deployment = submission.deployment
        objects = []
        for value in deployment.objects:
            if value.get("kind") not in {"ModelService", "FrontendService"} or not value.get("apiVersion", "").startswith("inference.foretoken.io/") or "spec" not in value:
                continue
            stopped = copy.deepcopy(value)
            stopped["spec"]["suspend"] = True
            objects.append(stopped)
        submission.apply(yaml.safe_dump_all(objects, sort_keys=False), patch_services=True)

        def report(elapsed: float, progress: ResourceProgress) -> None:
            print(f"[{elapsed:6.1f}s] {progress.resource.display_name}: {progress.state} — {progress.detail or progress.reason}", flush=True)

        def observe(resources: tuple[ResourceRef, ...], elapsed: float, budget: float) -> None:
            submission.check()

        resources = tuple(
            ResourceRef(value["kind"], value["metadata"]["name"], deployment.namespace)
            for value in objects
        )
        with kubectl.until(time.monotonic() + timeout_seconds(command.timeout)):
            if resources:
                wait_for_resources(resources, kubectl, command.timeout, report=report, observe=observe)
            submission.check()
        print("Foretoken services stopped; configuration, storage and build caches are retained")

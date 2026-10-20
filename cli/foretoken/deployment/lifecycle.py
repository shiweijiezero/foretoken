# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Submit named deployment intent and observe platform-owned controller and service lifecycles."""

from __future__ import annotations

import copy
import time
from contextlib import ExitStack
from dataclasses import replace
from typing import Any

import yaml

from foretoken.applications.files import SOURCE_REVISION
from foretoken.build.editable import EditableDeployment
from foretoken.build.state import release_source_deployment, source_operation
from foretoken.command_execution import CommandExecution
from foretoken.deployment.instances import DEPLOYMENT_RESOURCE, DeploymentInstance
from foretoken.deployment.manifest import (
    ForetokenDeployment,
    service_requires_execution,
)
from foretoken.deployment.progress import StartupProgress
from foretoken.deployment.status import report_progress, wait_for_resources
from foretoken.deployment.submission import DeploymentSubmission
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import (
    Kubectl,
    KubernetesPermissionError,
    ResourceRef,
    resource_ref,
    timeout_seconds,
)
from foretoken.kubernetes.mutation import DeploymentMutation
from foretoken.working_directory import resolve_runtime_directory


class DeploymentLifecycle:
    """Own CLI and benchmark submission while the platform owns controller startup and retirement."""

    def __init__(self, kubectl: Kubectl, *, temporary: bool = False) -> None:
        self.kubectl = kubectl
        self.temporary = temporary
        self.submission: DeploymentSubmission | None = None
        self.retained = False

    def apply(
        self, deployment: ForetokenDeployment, timeout: str, *,
        editable: str | None = None, engine_sources: tuple[str, ...] = (),
    ) -> ForetokenDeployment:
        """Publish selected applications, submit instance intent, and wait for its services."""
        with CommandExecution.current() or CommandExecution(timeout_seconds(timeout)):
            return self._apply(deployment, timeout, editable=editable, engine_sources=engine_sources)

    def delete(self, deployment: ForetokenDeployment, timeout: str) -> None:
        """Request instance retirement; its platform owner drains services before removing the controller."""
        with CommandExecution.current() or CommandExecution(timeout_seconds(timeout)):
            if deployment.uid:
                instance = DeploymentInstance(self.kubectl, deployment.namespace, deployment.name)
                instance.uid = deployment.uid
                mutation = DeploymentMutation(self.kubectl, deployment.namespace, deployment.name, timeout, uid=deployment.uid)
                self._delete_instance(instance, mutation, timeout)
            try:
                with CommandExecution(timeout_seconds(timeout)):
                    release_source_deployment(self.kubectl, deployment, timeout)
            except KubernetesPermissionError:
                print("Source build resources retained; repeat deletion with source-development access to reclaim them", flush=True)

    def _apply(
        self, deployment: ForetokenDeployment, timeout: str, *,
        editable: str | None, engine_sources: tuple[str, ...],
    ) -> ForetokenDeployment:
        """Publish instance intent and return the deployment observed after controller and service readiness."""
        started = time.monotonic()
        submission = DeploymentSubmission(self.kubectl, deployment, timeout, create_only=self.temporary)
        self.submission = submission
        submission.begin()
        deployment = submission.deployment
        objects = copy.deepcopy(deployment.objects)
        services = [
            obj for obj in objects
            if obj.get("kind") in {"ModelService", "FrontendService"}
            and obj.get("apiVersion", "").startswith("inference.foretoken.io/")
        ]
        for obj in services:
            obj["spec"].setdefault("suspend", False)
        deployment = deployment.with_objects(objects)
        stopping = [obj for obj in services if not service_requires_execution(obj)]
        runtime_required = len(stopping) != len(services)
        # Suspension takes effect before source compilation and does not select a new executable.
        if stopping:
            submission.apply(yaml.safe_dump_all(stopping, sort_keys=False))

        source = None
        with ExitStack() as locks:
            if editable is not None or engine_sources:
                locks.enter_context(source_operation(self.kubectl, timeout, required=True, shared=True))
                source = EditableDeployment.discover(self.kubectl, timeout, editable, engine_sources)
                assert source is not None
                locks.enter_context(source.operation(deployment, timeout))
                source.prepare(timeout)
                deployment = source.apply(deployment, timeout)
            revision = self.prepare_controller(
                submission, deployment, timeout,
                controller_source_revision=source.controller_source_revision if source is not None else "",
            )
            deployment = self._select_storage(deployment, submission.instance.access())
            objects = copy.deepcopy(deployment.objects)
            for obj in objects:
                if (
                    obj.get("kind") in {"ModelService", "FrontendService"}
                    and obj.get("apiVersion", "").startswith("inference.foretoken.io/")
                    and service_requires_execution(obj)
                ):
                    selected_source = obj["metadata"].get("annotations", {}).get(SOURCE_REVISION, "")
                    obj["spec"]["deploymentRevision"] = f"{revision}:{selected_source}"
            deployment = deployment.with_objects(objects)
            print(f"Applying deployment instance {deployment.name}", flush=True)
            submission.apply(deployment.rendered)

        print(f"Waiting up to {timeout} for Foretoken services", flush=True)
        rollout_started = time.monotonic()
        with self.kubectl.until(rollout_started + timeout_seconds(timeout)) as deadline, StartupProgress(
            self.kubectl, lambda line: print(line, flush=True)
        ) as startup:
            def observe(resources: tuple[ResourceRef, ...], elapsed: float, budget: float = 5.0) -> None:
                """Report progress only while this submission still owns the instance."""
                submission.check()
                startup.poll(resources, elapsed, budget)

            try:
                wait_for_resources(
                    deployment.service_refs(), self.kubectl, timeout,
                    report=report_progress, observe=observe,
                    expected_revisions={
                        resource_ref(obj): obj["spec"]["deploymentRevision"]
                        for obj in deployment.objects
                        if obj.get("kind") in {"ModelService", "FrontendService"}
                        and obj.get("apiVersion", "").startswith("inference.foretoken.io/")
                        and "deploymentRevision" in obj["spec"]
                    },
                )
            except DeploymentError:
                startup.poll(deployment.service_refs(), time.monotonic() - rollout_started, follow_logs=False, force=True)
                raise
            if source is not None and runtime_required:
                source.verify(
                    deployment, deadline,
                    observe=lambda: observe(deployment.service_refs(), time.monotonic() - rollout_started),
                )
            deployment = replace(submission.instance.deployment(), path=deployment.path)
            submission.check()
        print(f"Foretoken deployment completed in {time.monotonic() - started:.1f}s", flush=True)
        return deployment

    def prepare_controller(
        self, submission: DeploymentSubmission, deployment: ForetokenDeployment, timeout: str, *,
        controller_source_revision: str,
    ) -> str:
        """Request published controller code under the instance lock, then observe outside it."""
        with submission.mutation.hold():
            submission.check()
            submission.instance.request(
                deployment, timeout, update_id=submission.operation,
                controller_source_revision=controller_source_revision,
            )
        revision = submission.instance.wait_ready(timeout, observe=submission.check)
        submission.check()
        return revision

    def _select_storage(self, deployment: ForetokenDeployment, access: dict[str, Any]) -> ForetokenDeployment:
        """Resolve directory intent against the platform's authorized bindings without creating volumes."""
        objects = copy.deepcopy(deployment.objects)
        for document in objects:
            if document["kind"] != "RuntimeCache" or not document["apiVersion"].startswith("inference.foretoken.io/"):
                continue
            document["metadata"].get("annotations", {}).pop("inference.foretoken.io/directory-owner", None)
            if directory := document["spec"].get("directory"):
                assert deployment.path is not None
                resolved = str(resolve_runtime_directory(directory, deployment.path))
                if resolved not in access["directoryBindings"]:
                    raise DeploymentError(f"The platform has no prepared storage for directory {resolved}")
                document["spec"]["directory"] = resolved
        return deployment.with_objects(objects)

    def cleanup_temporary(
        self, deployment: ForetokenDeployment, timeout: str, *, retain_storage: bool = False,
    ) -> None:
        """Retire only this operation's creations, preserving retained captures and newer intent."""
        submission = self.submission
        if submission is None or not submission.created:
            return
        if not submission.owns_instance():
            print("Temporary deployment retained: another operation owns its instance", flush=True)
            return
        root = ResourceRef("InferenceDeployment", submission.instance.name, submission.namespace)
        if root in submission.created and not retain_storage:
            self._delete_instance(submission.instance, submission.mutation, timeout, submission=submission)
            return
        excluded = {"InferenceDeployment"}
        if retain_storage:
            excluded.update({"RuntimeCache", "PersistentVolumeClaim"})
        objects = tuple(obj for ref, obj in submission.created.items() if ref.kind not in excluded)
        self._remove_created(objects, timeout)
        if self.retained:
            return
        with submission.mutation.hold():
            if not submission.owns_instance():
                return
            current = submission.instance.read()
            removed = {resource_ref(obj) for obj in objects}
            retained = replace(submission.deployment, objects=tuple(
                obj for obj in submission.deployment.objects if resource_ref(obj) not in removed
            ))
            # Keep capture storage addressable and leave no serving intent. The
            # next temporary caller can atomically claim this retained instance.
            submission.instance.request(
                retained, timeout, update_id=current["spec"]["updateID"],
                controller_source_revision=current["spec"].get("controllerSourceRevision", ""),
            )

    def _remove_created(self, objects: tuple[dict[str, Any], ...], timeout: str) -> None:
        """Fence temporary deletion with instance intent and each observed object's UID/version."""
        submission = self.submission
        assert submission is not None
        ordered = sorted(objects, key=lambda obj: (resource_ref(obj) not in submission.expected, obj["kind"] == "RuntimeCache"))
        for obj in ordered:
            ref = resource_ref(obj)
            deadline = time.monotonic() + timeout_seconds(timeout)
            while True:
                with submission.mutation.hold():
                    if not submission.owns_instance():
                        self.retained = True
                        return
                    current = self.kubectl.get_if_exists(ref.kind, ref.name, ref.namespace)
                    if current is None:
                        break
                    deleting = current["metadata"].get("deletionTimestamp") and current["metadata"]["uid"] == obj["metadata"]["uid"]
                    if not deleting:
                        if not submission.owns_created(ref, current):
                            self.retained = True
                            return
                        try:
                            self.kubectl.delete_object(current, f"{max(1, int(deadline - time.monotonic()))}s", wait=False)
                        except DeploymentError:
                            latest = self.kubectl.get_if_exists(ref.kind, ref.name, ref.namespace)
                            if latest is None:
                                break
                            if latest["metadata"]["resourceVersion"] == current["metadata"]["resourceVersion"] or time.monotonic() >= deadline:
                                raise
                            continue
                self.kubectl.wait_deleted(ref.kind, current["metadata"], timeout)
                break

    def _delete_instance(
        self, instance: DeploymentInstance, mutation: DeploymentMutation, timeout: str, *,
        submission: DeploymentSubmission | None = None,
    ) -> None:
        """Invalidate pending submission without holding its mutation lock while finalizers drain."""
        current = instance.read(required=False)
        if current is None:
            return
        if current["metadata"].get("deletionTimestamp"):
            self.kubectl.wait_deleted(DEPLOYMENT_RESOURCE, current["metadata"], timeout)
            return
        with mutation.hold():
            if submission is None:
                mutation.supersede()
            elif not submission.owns_instance():
                self.retained = True
                return
            instance.delete(timeout, wait=False)
        instance.wait_deleted(timeout)

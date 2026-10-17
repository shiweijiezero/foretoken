# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own namespace controllers, service submission and temporary deployment cleanup."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from foretoken.editable import EditableDeployment
from foretoken.kubernetes import (
    Kubectl,
    resource_ref,
    timeout_seconds,
    wait_for_resources,
)
from foretoken.manifest import (
    DeploymentError,
    ForetokenDeployment,
    ResourceRef,
    parse_deployment,
    service_requires_execution,
)
from foretoken.namespace_mutation import NamespaceMutation
from foretoken.platform.config import default_platform_config
from foretoken.platform.helm import Helm
from foretoken.progress import StartupProgress
from foretoken.source_state import release_source_deployment, source_operation
from foretoken.storage import DirectoryVolumes
from foretoken.submission import _SERVICE_OPERATION_ANNOTATION, DeploymentSubmission


class DeploymentLifecycle:
    """Own controller preparation and resource retirement for CLI and benchmark callers."""

    def __init__(self, kubectl: Kubectl, *, temporary: bool = False) -> None:
        self.kubectl = kubectl
        self.helm = Helm(default_platform_config())
        self.volumes = DirectoryVolumes(kubectl)
        self.temporary = temporary
        self.submission: DeploymentSubmission | None = None
        self.controller_created: tuple[int, str, int] | None = None
        self.retained = False

    def apply(
        self,
        deployment: ForetokenDeployment,
        timeout: str,
        *,
        editable: str | None = None,
        engine_sources: tuple[str, ...] = (),
    ) -> ForetokenDeployment:
        """Prepare a namespace controller and converge submitted running or stopped service intent."""
        started = time.monotonic()
        timeout_seconds(timeout)
        namespace = deployment.namespace or self.kubectl.current_namespace()
        if not deployment.namespace:
            for document in deployment.objects:
                if document.get("apiVersion", "").startswith("inference.foretoken.io/"):
                    document["metadata"]["namespace"] = namespace
            deployment = parse_deployment(
                deployment.path, yaml.safe_dump_all(deployment.objects, sort_keys=False)
            )
        self.helm.deployment_release(namespace)
        submission = DeploymentSubmission(
            self.kubectl, deployment, timeout, create_only=self.temporary
        )
        self.submission = submission
        submission.begin()
        schema = json.loads(
            self.kubectl.get_raw(
                "/openapi/v3/apis/inference.foretoken.io/v1alpha1", timeout
            )
        )
        selection_kinds = {
            identity["kind"]
            for definition in schema["components"]["schemas"].values()
            for identity in definition.get("x-kubernetes-group-version-kind", [])
            if identity["kind"] in {"ModelService", "FrontendService"}
            and "deploymentRevision" in definition["properties"]["spec"]["properties"]
        }
        revision = uuid.uuid4().hex
        services = [
            obj
            for obj in deployment.objects
            if obj.get("kind") in {"ModelService", "FrontendService"}
            and obj.get("apiVersion", "").startswith("inference.foretoken.io/")
        ]
        for obj in services:
            obj["spec"].setdefault("suspend", False)
            if obj["kind"] in selection_kinds:
                obj["spec"]["deploymentRevision"] = revision
        deployment = parse_deployment(
            deployment.path, yaml.safe_dump_all(deployment.objects, sort_keys=False)
        )
        stopping = [obj for obj in services if not service_requires_execution(obj)]
        runtime_required = len(stopping) != len(services)
        # Retirement intent precedes build locks, so stopping never waits for source preparation.
        if stopping:
            submission.apply(yaml.safe_dump_all(stopping, sort_keys=False))
        # Builds publish complete applications before the controller and services select them.
        source = None
        with ExitStack() as locks:
            locks.enter_context(source_operation(self.kubectl, timeout, shared=True))
            values = self.helm.deployment_values(self.kubectl)
            if not self.temporary:
                source = EditableDeployment.discover(
                    self.kubectl, editable, engine_sources
                )
                if source is not None:
                    locks.enter_context(source.operation(deployment, timeout))
                    source.prepare(timeout)
                    deployment = source.apply(deployment, timeout)
                    values = source.deployment_values(values)
            self.prepare_controller(
                submission,
                timeout,
                values=values,
                source_root=source.root if source is not None else None,
                reuse_existing=self.temporary,
            )
            if runtime_required:
                print(
                    f"Applying {deployment.path} to namespace {namespace}", flush=True
                )
                self.volumes.apply(deployment, timeout, submit=submission.apply)
        # Readiness observation releases build locks so another operation can stop the service.
        print(f"Waiting up to {timeout} for Foretoken services", flush=True)
        rollout_started = time.monotonic()
        with StartupProgress(
            self.kubectl, lambda line: print(line, flush=True)
        ) as startup:

            def observe(
                resources: tuple[ResourceRef, ...], elapsed: float, budget: float = 5.0
            ) -> None:
                """Report startup only while this submission still owns the service intent."""
                submission.check()
                startup.poll(resources, elapsed, budget)

            try:
                wait_for_resources(
                    deployment.service_refs(),
                    self.kubectl,
                    timeout,
                    report=lambda elapsed, progress: print(
                        f"[{elapsed:6.1f}s] {progress.resource.display_name:<48} "
                        f"{progress.state}"
                        + (f" — {progress.detail}" if progress.detail else ""),
                        flush=True,
                    ),
                    observe=observe,
                    expected_revisions={
                        ref: revision
                        for ref in deployment.service_refs()
                        if ref.kind in selection_kinds
                    },
                )
            except DeploymentError:
                startup.poll(
                    deployment.service_refs(),
                    time.monotonic() - rollout_started,
                    follow_logs=False,
                    force=True,
                )
                raise
            if source is not None and runtime_required:
                source.verify(
                    deployment,
                    timeout,
                    observe=lambda: observe(
                        deployment.service_refs(), time.monotonic() - rollout_started
                    ),
                )
        submission.check()
        print(
            f"Foretoken deployment completed in {time.monotonic() - started:.1f}s",
            flush=True,
        )
        return deployment

    def prepare_controller(
        self,
        submission: DeploymentSubmission,
        timeout: str,
        *,
        values: dict[str, Any] | None = None,
        source_root: Path | None = None,
        reuse_existing: bool = False,
    ) -> None:
        """Prepare reconciliation for deploy, stop or temporary evaluation without duplicating its lifecycle."""
        self.submission = submission
        namespace = submission.namespace
        release = self.helm.deployment_release(namespace)
        with submission.mutation.hold():
            submission.check()
            controllers = self.kubectl.list_resources(
                ("deployments",),
                namespace,
                label_selector="app.kubernetes.io/name=foretoken-control-plane",
            )
            if not reuse_existing or not controllers:
                existed = self.helm.release_exists(release)
                try:
                    self.helm.install_deployment(
                        release,
                        values
                        if values is not None
                        else self.helm.deployment_values(self.kubectl),
                        timeout,
                        source_root=source_root,
                        operation=submission.operation,
                    )
                finally:
                    if self.temporary and not existed:
                        self._record_controller(namespace)
        self.helm.wait_control_plane(release, timeout)
        submission.check()

    def _record_controller(self, namespace: str) -> None:
        """Retain native Helm and Deployment identities, including a partially successful installation."""
        release = self.helm.deployment_release(namespace)
        if not self.helm.release_exists(release):
            return
        if not self.helm.has_release_label(
            release, _SERVICE_OPERATION_ANNOTATION, self.submission.operation
        ):
            return
        status = json.loads(
            self.helm.run(
                ["status", release.name, "-n", namespace, "-o", "json"]
            ).stdout
        )
        controller = self.kubectl.get_if_exists(
            "deployment", release.name + "-control-plane", namespace
        )
        metadata = controller["metadata"] if controller is not None else {}
        self.controller_created = (
            status["version"],
            metadata.get("uid", ""),
            metadata.get("generation", 0),
        )

    def cleanup_temporary(
        self,
        deployment: ForetokenDeployment,
        timeout: str,
        *,
        retain_storage: bool = False,
    ) -> None:
        """Delete only actual creations whose namespace and object intent have not been taken over."""
        submission = self.submission
        if submission is None or (
            not submission.created and self.controller_created is None
        ):
            return
        if not submission.owns_namespace():
            print(
                "Temporary deployment retained: a newer operation owns its namespace",
                flush=True,
            )
            return
        # A namespace can acquire unrelated resources after this operation creates it.
        excluded = (
            {"Namespace", "RuntimeCache", "PersistentVolumeClaim"}
            if retain_storage
            else {"Namespace"}
        )
        objects = tuple(
            obj for ref, obj in submission.created.items() if ref.kind not in excluded
        )
        selected = {resource_ref(obj) for obj in objects}
        caches = tuple(
            cache
            for cache in deployment.runtime_caches
            if ResourceRef("RuntimeCache", cache.name, cache.namespace) in selected
        )
        created = replace(
            deployment,
            objects=objects,
            runtime_caches=caches,
            rendered=yaml.safe_dump_all(objects, sort_keys=False),
        )
        if objects:
            self.volumes.delete(
                created,
                timeout,
                remove=lambda values: self._remove_created(values, timeout),
            )
        with submission.mutation.hold():
            if (
                self.retained
                or not submission.owns_namespace()
                or self._remaining_resources(submission.namespace)
            ):
                return
            if self.controller_created is not None:
                release = self.helm.deployment_release(submission.namespace)
                if self.helm.release_exists(release):
                    status = json.loads(
                        self.helm.run(
                            [
                                "status",
                                release.name,
                                "-n",
                                release.namespace,
                                "-o",
                                "json",
                            ]
                        ).stdout
                    )
                    controller = self.kubectl.get_if_exists(
                        "deployment", release.name + "-control-plane", release.namespace
                    )
                    metadata = controller["metadata"] if controller is not None else {}
                    identity = (
                        status["version"],
                        metadata.get("uid", ""),
                        metadata.get("generation", 0),
                    )
                    if identity != self.controller_created:
                        return
                    self.helm.remove_deployment(release, timeout)

    def _remove_created(
        self, objects: tuple[dict[str, Any], ...], timeout: str
    ) -> set[str]:
        """Fence each temporary deletion with its current namespace, intent, UID and API version."""
        removed = set()
        ordered = sorted(
            objects,
            key=lambda obj: (
                resource_ref(obj) not in self.submission.expected,
                obj["kind"] == "RuntimeCache",
            ),
        )
        for obj in ordered:
            ref = resource_ref(obj)
            deadline = time.monotonic() + timeout_seconds(timeout)
            while True:
                with self.submission.mutation.hold():
                    if not self.submission.owns_namespace():
                        self.retained = True
                        return removed
                    current = self.kubectl.get_if_exists(
                        ref.kind, ref.name, ref.namespace
                    )
                    if current is None:
                        removed.add(obj["metadata"]["uid"])
                        break
                    deleting = (
                        current["metadata"].get("deletionTimestamp")
                        and current["metadata"]["uid"] == obj["metadata"]["uid"]
                    )
                    if not deleting:
                        if not self.submission.owns_created(ref, current):
                            self.retained = True
                            return removed
                        try:
                            self.kubectl.delete_object(
                                current,
                                f"{max(1, int(deadline - time.monotonic()))}s",
                                wait=False,
                            )
                        except DeploymentError:
                            latest = self.kubectl.get_if_exists(
                                ref.kind, ref.name, ref.namespace
                            )
                            if latest is None:
                                removed.add(obj["metadata"]["uid"])
                                break
                            if (
                                latest["metadata"]["resourceVersion"]
                                == current["metadata"]["resourceVersion"]
                                or time.monotonic() >= deadline
                            ):
                                raise
                            continue
                self.kubectl.wait_deleted(ref.kind, current["metadata"], timeout)
                removed.add(current["metadata"]["uid"])
                break
        return removed

    def delete(self, deployment: ForetokenDeployment, timeout: str) -> None:
        """Drain an explicitly deleted deployment before removing its controller and namespace."""
        timeout_seconds(timeout)
        namespace = deployment.namespace or self.kubectl.current_namespace()
        with (
            source_operation(self.kubectl, timeout),
            NamespaceMutation(self.kubectl, namespace, timeout).hold(),
        ):
            self._delete(deployment, timeout)

    def _delete(self, deployment: ForetokenDeployment, timeout: str) -> None:
        """Keep the finalizer owner alive until all namespace resources have retired."""
        namespace_objects = tuple(
            obj for obj in deployment.objects if obj.get("kind") == "Namespace"
        )
        objects = tuple(
            obj for obj in deployment.objects if obj.get("kind") != "Namespace"
        )
        resources = replace(
            deployment,
            objects=objects,
            rendered=yaml.safe_dump_all(objects, sort_keys=False),
        )
        self.volumes.delete(resources, timeout)
        namespace = deployment.namespace or self.kubectl.current_namespace()
        if namespace_objects:
            for kind in (
                "profileruns",
                "videotasks",
                "frontendservices",
                "modelservices",
                "kvservices",
                "runtimecaches",
            ):
                self.kubectl.run(
                    [
                        "delete",
                        kind + ".inference.foretoken.io",
                        "--all",
                        "-n",
                        namespace,
                        "--ignore-not-found",
                        "--wait=true",
                        f"--timeout={timeout}",
                    ]
                )
        remaining = self._remaining_resources(namespace)
        if remaining:
            if namespace_objects:
                deadline = time.monotonic() + timeout_seconds(timeout)
                while remaining:
                    if time.monotonic() >= deadline:
                        names = ", ".join(
                            obj["kind"] + "/" + obj["metadata"]["name"]
                            for obj in remaining
                        )
                        raise DeploymentError(
                            f"waiting for controller cleanup in {namespace}: {names}"
                        )
                    time.sleep(1)
                    remaining = self._remaining_resources(namespace)
            else:
                release_source_deployment(self.kubectl, deployment, timeout)
                return
        self.helm.remove_deployment(self.helm.deployment_release(namespace), timeout)
        if namespace_objects:
            self.kubectl.delete(
                yaml.safe_dump_all(namespace_objects, sort_keys=False), timeout
            )
        release_source_deployment(self.kubectl, deployment, timeout)

    def _remaining_resources(self, namespace: str) -> tuple[dict[str, Any], ...]:
        """Keep the namespace controller while any Foretoken resource still needs reconciliation."""
        kinds = self.kubectl.api_resource_names("inference.foretoken.io")
        return self.kubectl.list_resources(tuple(kinds), namespace) if kinds else ()

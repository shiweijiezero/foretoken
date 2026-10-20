# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare and retire platform-owned application storage, lending publication handles to callers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from foretoken.applications.files import ApplicationFiles, application_references
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl
from foretoken.kubernetes.documents import encode_resource, encode_resources
from foretoken.platform.helm_client import owned_by_release

if TYPE_CHECKING:
    from foretoken.platform.helm import Helm


class PlatformApplicationFiles:
    """Own Chart bootstrap and adoption; ApplicationFiles borrows the ready origin to publish payloads."""

    def __init__(self, helm: Helm, kubectl: Kubectl) -> None:
        self._helm = helm
        self._kubectl = kubectl
        self._release = helm.platform_release()

    def prepare(self, rendered: tuple[dict[str, Any], ...], timeout: str) -> ApplicationFiles | None:
        """Prepare chart-owned storage and return a borrowed origin, or None when no storage is declared."""
        documents = self._resources(rendered)
        if not documents:
            return None
        release = self._release
        kubectl = self._kubectl
        if not kubectl.exists("namespace", release.namespace):
            kubectl.run(["create", "namespace", release.namespace])
        operations = []
        for document in documents:
            metadata = document["metadata"]
            current = kubectl.get_if_exists(document["kind"], metadata["name"], release.namespace)
            if current is not None:
                if not owned_by_release(current, release):
                    raise DeploymentError(
                        f"{document['kind']}/{metadata['name']} is not owned by Helm release {release.display_name}"
                    )
                metadata["resourceVersion"] = current["metadata"]["resourceVersion"]
            metadata.setdefault("annotations", {}).update({
                "meta.helm.sh/release-name": release.name,
                "meta.helm.sh/release-namespace": release.namespace,
            })
            operations.append(("create" if current is None else "apply", document))
        # Verify every existing object before writing. Native preconditions prevent
        # taking over an object replaced during bootstrap.
        for operation, document in operations:
            kubectl.run([operation, "-f", "-"], input_text=encode_resource(document))
        origin = ApplicationFiles(kubectl, release.namespace)
        origin.prepare(timeout)
        return origin

    @contextmanager
    def adopt(self, rendered: tuple[dict[str, Any], ...], timeout: str) -> Iterator[ApplicationFiles | None]:
        """Hold release publication until Helm adopts the prepared origin and selects its payload."""
        origin = self.prepare(rendered, timeout)
        if origin is None:
            yield
            return
        configuration = next(
            document["data"] for document in self._resources(rendered)
            if document["kind"] == "ConfigMap"
        )
        if not configuration.get("releaseURL"):
            yield origin
            return

        def retained() -> set[str] | None:
            """Read consumers and rollback history only after publisher ownership is established."""
            references = origin.references(self._helm.application_history())
            if references is not None:
                references.update(application_references(rendered))
                references.add(configuration["revision"])
            return references

        def previous() -> dict[str, str]:
            """Borrow installed component directories for immutable-file reuse."""
            references = application_references(self._helm.installed_chart_manifest(self._release))
            prefix = origin.endpoint.rstrip("/") + "/"
            return {
                component: revision for reference in sorted(references)
                if reference.startswith(prefix)
                for component, revision in [reference.removeprefix(prefix).rstrip("/").split("/", 1)]
            }

        with origin.import_release(
            configuration["releaseURL"], configuration["revision"], retained, previous,
            timeout=timeout, credentials_secret=configuration["credentialsSecret"],
        ):
            yield origin

    def resources(self) -> tuple[dict[str, Any], ...]:
        """Find native origin objects still owned by the platform, including failed bootstraps."""
        release = self._release
        if not self._kubectl.exists("namespace", release.namespace):
            return ()
        return tuple(
            document for document in self._kubectl.list_resources(
                ("persistentvolumeclaims", "configmaps", "deployments", "services"),
                release.namespace, label_selector="app.kubernetes.io/name=foretoken-application-files",
            )
            if owned_by_release(document, release)
        )

    def retire(self, resources: tuple[dict[str, Any], ...], timeout: str) -> None:
        """Stop PVC-owned writers before explicit uninstall removes an unadopted origin."""
        if not resources:
            return
        release = self._release
        claims = {
            document["metadata"]["uid"] for document in resources
            if document["kind"] == "PersistentVolumeClaim"
        }
        for job in self._kubectl.list_resources(
            ("jobs",), release.namespace, label_selector="foretoken.io/application-files=publisher",
        ):
            if any(
                owner["kind"] == "PersistentVolumeClaim" and owner["uid"] in claims
                for owner in job["metadata"].get("ownerReferences", [])
            ):
                self._kubectl.run([
                    "delete", "job", job["metadata"]["name"], "-n", release.namespace,
                    "--cascade=foreground", "--wait=true", "--timeout=" + timeout,
                ])
        self._kubectl.delete(encode_resources(resources), timeout)

    @staticmethod
    def _resources(rendered: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        return tuple(
            document for document in rendered
            if document["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "foretoken-application-files"
        )

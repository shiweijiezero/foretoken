# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare administrator-owned namespace controllers, access and shared model storage."""

from __future__ import annotations

from contextlib import ExitStack

from foretoken.arguments import NamespaceCommand
from foretoken.editable import EditableDeployment
from foretoken.kubernetes import Kubectl, resolve_gateway_access
from foretoken.manifest import DeploymentError, ResourceRef
from foretoken.namespace_access import NamespaceAccess, namespace_controller_name
from foretoken.namespace_mutation import NamespaceMutation
from foretoken.nfs_cache import prepare_shared_cache
from foretoken.platform.config import default_platform_config, load_platform_values
from foretoken.platform.helm import Helm, _merge_values
from foretoken.source_state import release_source_namespace, source_operation


class NamespaceLifecycle:
    """Own namespace preparation while service members only consume its controller and storage."""

    def __init__(self) -> None:
        self.kubectl = Kubectl()
        self.helm = Helm(default_platform_config())

    def install(self, command: NamespaceCommand) -> None:
        """Prepare or update one namespace without reinstalling the shared Kubernetes platform."""
        namespace, timeout = command.namespace, command.timeout
        release = self.helm.deployment_release(namespace)
        overrides = load_platform_values(command.values)
        with ExitStack() as operations:
            operations.enter_context(source_operation(self.kubectl, timeout, shared=True))
            platform_values = self.helm.deployment_values(self.kubectl)
            shared_cache = (
                prepare_shared_cache(self.kubectl, command.shared_cache, timeout)
                if command.shared_cache is not None else None
            )
            if self.kubectl.get_if_exists("namespace", namespace) is None:
                try:
                    self.kubectl.run(["create", "namespace", namespace])
                except DeploymentError as error:
                    if "(AlreadyExists)" not in str(error):
                        raise
            mutation = NamespaceMutation(self.kubectl, namespace, timeout)
            namespace_record = mutation.prepare()
            source = None
            if command.editable is not None:
                source = EditableDeployment.discover(self.kubectl, command.editable)
                operations.enter_context(source.operation(
                    (ResourceRef("Deployment", namespace_controller_name(), namespace),), timeout,
                ))
                source.prepare_namespace(timeout)
            if self.helm.release_exists(release):
                if not self.helm.is_cli_managed(release):
                    raise DeploymentError(f"Helm release {release.display_name} is not managed by foretoken")
                values = self.helm._release_values(release)
            else:
                values = platform_values
            for override in overrides:
                values = _merge_values(values, override)
            if source is not None:
                values = source.namespace_values(values)
            access = values.setdefault("namespaceAccess", {})
            access["enabled"] = True
            nodes = self.kubectl.list_cluster_resources(("nodes",))
            active_nodes = [node for node in nodes if not node.get("spec", {}).get("unschedulable")]
            access["cacheAccessMode"] = "ReadWriteOnce" if len(active_nodes) == 1 else "ReadWriteMany"
            if shared_cache is not None:
                access["sharedCache"] = shared_cache
                values["workload"]["cache"]["claimName"] = release.name + "-shared-cache"
                access["cacheAccessMode"] = "ReadWriteMany"
            access["gateway"] = {}
            if values["frontend"]["mode"] == "gateway":
                gateway = values["frontend"]["gateway"]
                access["gateway"] = resolve_gateway_access(
                    self.kubectl, gateway["name"], gateway["namespace"], gateway["sectionName"], timeout,
                )
            # Build and readiness waits leave service stops available; only mutation is serialized.
            with mutation.hold():
                current_record = self.kubectl.get("configmap", mutation.name, namespace)
                if current_record["metadata"]["uid"] != namespace_record["metadata"]["uid"]:
                    raise DeploymentError(f"Namespace {namespace} was recreated during preparation")
                mutation.supersede()
                self.helm.install_deployment(
                    release, values, timeout, source_root=source.root if source is not None else None,
                )
            self.helm.wait_control_plane(release, timeout)
            claim = values["workload"]["cache"]["claimName"]
            if claim:
                self.kubectl.run([
                    "wait", "pvc/" + claim, "--namespace", namespace,
                    "--for=jsonpath={.status.phase}=Bound", f"--timeout={timeout}",
                ])
            if NamespaceAccess.read(self.kubectl, namespace) is None:
                raise DeploymentError("The installed chart does not support namespace access; update the shared platform")
        role = namespace_controller_name() + "-operator"
        print(f"Namespace {namespace} is ready; bind members to Role/{role} in this namespace", flush=True)

    def uninstall(self, command: NamespaceCommand) -> None:
        """Remove an unused namespace controller and its access resources, retaining shared model data."""
        namespace, timeout = command.namespace, command.timeout
        release = self.helm.deployment_release(namespace)
        mutation = NamespaceMutation(self.kubectl, namespace, timeout)
        with source_operation(self.kubectl, timeout):
            installed = self.helm.release_exists(release)
            record = self.kubectl.get_if_exists("configmap", mutation.name, namespace)
            if not installed and record is None:
                release_source_namespace(self.kubectl, namespace, timeout)
                print(f"No Foretoken controller is installed in namespace {namespace}", flush=True)
                return
            if record is not None and record["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != "foretoken":
                raise DeploymentError(f"Namespace operation record in {namespace} belongs to another owner")
            if record is None:
                mutation.prepare()
            with mutation.hold():
                resources = self.kubectl.api_resource_names("inference.foretoken.io")
                remaining = self.kubectl.list_resources(resources, namespace) if resources else ()
                if remaining:
                    names = ", ".join(value["kind"] + "/" + value["metadata"]["name"] for value in remaining)
                    raise DeploymentError(f"Delete Foretoken services and caches in {namespace} before uninstalling: {names}")
                mutation.supersede()
                self.helm.remove_deployment(release, timeout)
                release_source_namespace(self.kubectl, namespace, timeout)
                record = self.kubectl.get("configmap", mutation.name, namespace)
                self.kubectl.delete_object(record, timeout)
        print(f"Foretoken controller removed from {namespace}; the namespace and shared model files are retained", flush=True)

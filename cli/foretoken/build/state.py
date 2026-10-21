# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own source bindings, operation locks, durable state and artifact retirement."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

import yaml
from loguru import logger

from foretoken.applications.files import (
    APPLICATION_PUBLISHER_BINDING_LABEL,
    SERVICE_COMPONENTS,
    SOURCE_REVISION,
    ApplicationFiles,
    application_consumers,
    remove_application_jobs,
)
from foretoken.arguments import InstallCommand
from foretoken.build.cluster.builder import remove_build_pods
from foretoken.build.cluster.images import (
    SOURCE_BUILD_BINDING_LABEL,
    SOURCE_BUILD_POD_LABEL,
    SOURCE_IMAGE_HELPER_LABEL,
    image_tools_image,
    node_containerd_socket,
)
from foretoken.build.cluster.storage import SOURCE_BUILD_CACHE_LABEL
from foretoken.build.inputs import BuildInputs
from foretoken.build.records import (
    SOURCE_BUILD_KEY,
    WORKSPACE_LABEL,
    WORKSPACE_SELECTION_KEY,
    retain_workspace,
)
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.processes.execution import CommandExecution, cleanup_commands

SOURCE_INSTALL_ANNOTATION = "foretoken.io/install-source"


def _source_home() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "foretoken"
        / "source"
    )


def context_identity(kubectl: Kubectl) -> dict[str, str]:
    """Read connection identity locally without adding API permissions to release deploys."""
    return {"server": kubectl.server}


def cluster_state_directory(kubectl: Kubectl) -> Path:
    """Keep workstation paths local and bind them to the actual cluster identity."""
    cluster = kubectl.get("namespace", "kube-system")["metadata"]["uid"]
    return _source_home() / cluster


def has_server_binding(kubectl: Kubectl) -> bool:
    """Prefilter local bindings without requiring cluster access for release deployments."""
    bindings = [
        *(_source_home().glob("*/install.json")),
        *(_source_home().glob("*/build.json")),
        *(_source_home().glob("*/checkouts/*/install.json")),
        *(_source_home().glob("*/checkouts/*/build.json")),
        *(_source_home().glob("*/checkouts/*/checkout.json")),
    ]
    if not bindings:
        return False
    server = context_identity(kubectl)["server"]
    for path in bindings:
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            # An uninstall may retire a binding before this operation acquires its lock.
            continue
        if state.get("context", {}).get("server") == server:
            return True
    return False


def _acquire_source_lock(lock: TextIO, timeout: str, description: str, *, shared: bool = False) -> None:
    """Bound lock acquisition without expiring a live source operation's ownership."""
    import fcntl

    deadline = time.monotonic() + timeout_seconds(timeout)
    reported = False
    while True:
        if execution := CommandExecution.current():
            execution.check()
        try:
            fcntl.flock(lock, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeploymentError(f"Timed out after {timeout} waiting for {description}") from None
            if not reported:
                logger.info("Waiting for {} (up to {})", description, timeout)
                reported = True
            time.sleep(min(0.2, remaining))


@contextmanager
def source_operation(
    kubectl: Kubectl, timeout: str, *, required: bool = False, shared: bool = False,
    prepare: bool = True,
) -> Iterator[None]:
    """Coordinate source builds and platform changes on a workstation.

    Installation prepares recorded outputs under the exclusive lock. Uninstall
    uses only the lock; its resource ownership comes from the cluster's origin.
    """
    if not required and not has_server_binding(kubectl):
        yield
        return
    # POSIX locking is needed only for source operations, not other CLI commands.
    import fcntl

    directory = cluster_state_directory(kubectl)
    locks = _source_home() / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    # NFS shared locks require a readable descriptor; this file serves both lock modes.
    with (locks / directory.name).open("a+") as lock:
        _acquire_source_lock(lock, timeout, "the platform source operation", shared=shared)
        try:
            if shared or not prepare:
                yield
                return
            bindings = set()
            for name in ("install.json", "build.json"):
                path = directory / name
                if path.is_file():
                    binding = (
                        json.loads(path.read_text()).get("build", {}).get("binding")
                    )
                    if binding:
                        bindings.add(binding)
            for binding in bindings:
                remove_application_jobs(kubectl, binding, timeout)
                remove_build_pods(kubectl, timeout, binding=binding)
            # Protect persisted source outputs before any compiler or workload can need them.
            reconcile_source_resources(kubectl, timeout)
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def checkout_state_directory(cluster: Path, root: Path, timeout: str, *, context: dict[str, str]) -> Path:
    """Register a source directory independently of its deployment instances under the workspace registry lock."""
    checkouts = cluster / "checkouts"
    checkouts.mkdir(parents=True, exist_ok=True)
    record = {"root": str(root), "context": context}
    with (checkouts / ".lock").open("a+") as lock:
        _acquire_source_lock(lock, timeout, "source checkout registration")
        for identity in checkouts.glob("*/checkout.json"):
            previous = json.loads(identity.read_text())
            if previous["root"] == str(root):
                if previous != record:
                    write_source_state(identity, record)
                return identity.parent
        directory = checkouts / uuid.uuid4().hex
        directory.mkdir()
        write_source_state(directory / "checkout.json", record)
        return directory


@contextmanager
def checkout_operation(directory: Path, kubectl: Kubectl, timeout: str) -> Iterator[None]:
    """Own a checkout's build outputs until their service selections have been submitted."""
    with (directory / ".lock").open("a+") as lock:
        _acquire_source_lock(lock, timeout, "this checkout's source operation")
        remove_application_jobs(kubectl, directory.name, timeout)
        remove_build_pods(kubectl, timeout, binding=directory.name)
        yield


def write_source_state(path: Path, value: object) -> None:
    """Replace one local source state only after its complete payload has been written."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def capture_build_inputs(
    root: Path, engines: dict[str, str] | None = None,
    previous: Path | None = None, *, mooncake_version: str | None = None,
) -> Iterator[Path]:
    """Capture candidate inputs until their source owner adopts the snapshot into saved state."""
    destination = _source_home() / "builds" / ("inputs-" + uuid.uuid4().hex)
    try:
        inputs = BuildInputs.select(root, engines, mooncake_version=mooncake_version)
        inputs.capture(destination, previous=previous)
        yield destination
    finally:
        if destination.exists():
            shutil.rmtree(destination)


@contextmanager
def local_candidates(directory: Path) -> Iterator[None]:
    """Retire inputs outside workspace and installed-platform selections under the owning source lock.

    Running workloads consume independent publications rather than local snapshots.
    """
    try:
        yield
    finally:
        retained = {
            json.loads(path.read_text()).get("inputs")
            for name in ("install.json", "build.json")
            if (path := directory / name).is_file()
        }
        selection = directory.parent.parent / "install.json"
        if (
            directory.parent.name == "checkouts" and selection.is_file()
            and (installed := json.loads(selection.read_text()).get("inputs"))
        ):
            selected = Path(installed)
            if selected.parent == Path("checkouts") / directory.name:
                retained.add(selected.name)
        for snapshot in directory.glob("inputs-*"):
            if snapshot.name not in retained:
                shutil.rmtree(snapshot)
        if (directory / "bundles").is_dir():
            shutil.rmtree(directory / "bundles")


def forget_install(kubectl: Kubectl) -> None:
    """Remove this workstation's cluster registry after explicit platform uninstall, when one exists."""
    if has_server_binding(kubectl):
        directory = cluster_state_directory(kubectl)
        if directory.exists():
            shutil.rmtree(directory)


def runtime_settings(platform: dict[str, Any]) -> dict[str, Any]:
    """Read installed application defaults without depending on a serving controller."""
    values = json.loads(platform["data"]["deploymentValues"])
    image = values["image"]
    controller_image = image["repository"] + (
        "@" + image["digest"] if image.get("digest") else ":" + image["tag"]
    )
    runtime = values["runtime"]
    workload = values["workload"]
    return {
        "control_plane_image": controller_image,
        "image": values["frontend"]["image"],
        "model_image": runtime["vllm"]["image"],
        "omni_image": runtime["vllmOmni"]["image"],
        "nsight_image": runtime["vllm"]["nsightImage"],
        "mount": workload["cache"]["mountPath"],
        "claim": workload["cache"]["claimName"],
        "pull_secrets": [item["name"] for item in workload["imagePullSecrets"]],
    }


def resolve_source_base_image(
    kubectl: Kubectl,
    root: Path,
    requested_image: str | None,
    saved_image: str | None = None,
) -> str | None:
    """Select explicit base intent from requests or this checkout's installed and pending state."""
    if requested_image is not None:
        return None if requested_image == "auto" else requested_image or None
    if saved_image is not None:
        return saved_image
    directory = cluster_state_directory(kubectl)
    for parent in [*(directory / "checkouts").glob("*"), directory]:
        for name in ("install.json", "build.json"):
            path = parent / name
            if path.is_file():
                state = json.loads(path.read_text())
                if state.get("root") == str(root):
                    return state.get("base_image")
    return None


def record_install(
    kubectl: Kubectl,
    command: InstallCommand,
    base_image: str | None,
    snapshot: Path,
    *,
    build_state: dict[str, Any],
) -> None:
    """Commit the directory's completed workspace and the platform's installed default selection."""
    if command.editable is None:
        return
    root = Path(command.editable).expanduser().resolve()
    directory = cluster_state_directory(kubectl)
    workspace = snapshot.parent
    engines = json.loads((workspace / "build.json").read_text())["engines"]
    settings = asdict(command)
    settings.update(
        editable=str(root),
        values=[],
        engine_sources=[f"{name}={path}" for name, path in engines.items()],
    )
    from foretoken.platform.config import default_platform_config
    from foretoken.platform.helm import Helm

    platform = Helm(default_platform_config(command.oci_registry), kubectl).platform_configuration()
    if platform["metadata"].get("annotations", {}).get(SOURCE_INSTALL_ANNOTATION) != "source":
        raise DeploymentError("expected a source-installed Foretoken platform")
    # Other workstations adopt immutable outputs, never this directory's compiler volume.
    recipe = {
        "registry": build_state["registry"],
        "ociRegistry": command.oci_registry,
        "baseImage": base_image,
        "runtimeBackend": build_state["runtime_backend"],
        "buildMetaXRuntime": build_state["build_metax_runtime"],
        "configuration": build_state["configuration"],
        "completedBuild": build_state["completed"],
    }
    metadata = platform["metadata"]
    kubectl.run([
        "patch", "configmap", metadata["name"], "--namespace", metadata["namespace"],
        "--type=merge", "--patch", json.dumps({
            "metadata": {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]},
            "data": {SOURCE_BUILD_KEY: json.dumps(recipe)},
        }),
    ])
    path = workspace / "install.json"
    previous = json.loads(path.read_text()) if path.is_file() else {}
    applications = build_state["applications"]
    # Preserve matching service selections without retaining partial-build metadata.
    bundles = {
        component: {"revision": bundle["revision"]}
        for component, bundle in previous.get("bundles", {}).items()
        if bundle["revision"] == applications.get(component, {}).get("revision")
    }
    state = {
        "root": str(root),
        "engines": engines,
        "build": build_state,
        "context": context_identity(kubectl),
        "inputs": snapshot.name,
        "platform_uid": platform["metadata"]["uid"],
        "base_image": base_image,
        "runtime": runtime_settings(platform),
        "command": settings,
        "bundles": bundles,
    }
    with local_candidates(workspace):
        write_source_state(path, state)
        (workspace / "build.json").unlink(missing_ok=True)
    # The platform record selects a completed workspace; it does not own another
    # source tree, compiler binding, or incremental target directory.
    with local_candidates(directory):
        write_source_state(directory / "install.json", {
            **state, "inputs": str(snapshot.relative_to(directory)),
        })
        (directory / "build.json").unlink(missing_ok=True)
    origin = ApplicationFiles(kubectl, metadata["namespace"])
    origin.prepare(command.timeout)
    retain_workspace(
        origin, build_state["binding"],
        (origin.reference(component, value["revision"]) for component, value in applications.items()),
        build_state["images"].values(),
    )


def application_images(documents: Iterable[dict[str, Any]]) -> set[str]:
    """Read environment images selected by platform defaults, services and workload consumers."""
    images: set[str] = set()
    for document in documents:
        if document.get("kind") == "ConfigMap" and WORKSPACE_SELECTION_KEY in document.get("data", {}):
            images.update(json.loads(document["data"][WORKSPACE_SELECTION_KEY])["images"])
        if document.get("kind") == "ConfigMap" and "controller-selection.json" in document.get("data", {}):
            selection = json.loads(document["data"]["controller-selection.json"])
            images.update(application_images([selection["template"]["controller"]]))
        if document.get("metadata", {}).get("labels", {}).get("foretoken.io/platform") == "configuration":
            runtime = runtime_settings(document)
            if source_build := document["data"].get(SOURCE_BUILD_KEY):
                images.update(json.loads(source_build).get("completedBuild", {}).get("images", {}).values())
            images.update(filter(None, (
                runtime["control_plane_image"], runtime["image"], runtime["model_image"],
                runtime["omni_image"], runtime["nsight_image"],
            )))
        spec = document.get("spec", {})
        status = document.get("status", {})
        template = spec.get("template", {})
        pod_spec = template.get("spec", spec)
        selections = [
            status.get("application", {}),
            template.get("application", {}),
            spec.get("runtime", {}),
            status.get("plan", {}),
            *status.get("poolApplications", {}).values(),
        ]
        for selection in selections:
            if selection:
                for key in ("image", "workerImage"):
                    if selection.get(key):
                        images.add(selection[key])
        for key in ("containers", "initContainers", "ephemeralContainers"):
            for container in pod_spec.get(key, []):
                images.add(container["image"])
                if container["name"] != "manager":
                    continue
                for argument in container.get("args", []):
                    for prefix in (
                        "--frontend-image=",
                        "--inference-engine-image=",
                        "--omni-inference-engine-image=",
                        "--video-worker-image=",
                        "--nsight-image=",
                    ):
                        if argument.startswith(prefix) and argument.removeprefix(
                            prefix
                        ):
                            images.add(argument.removeprefix(prefix))
    return images


def _source_image_access(
    pods: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], tuple[str, tuple[str, ...]]]:
    """Read node runtime access from actual builder Pods, without opening workstation credentials."""
    access = {}
    for pod in pods:
        spec = pod["spec"]
        node = spec.get("nodeName")
        socket = next(
            (volume["hostPath"]["path"] for volume in spec.get("volumes", []) if volume["name"] == "runtime-socket"),
            None,
        )
        images = next((container for container in spec["containers"] if container["name"] == "images"), None)
        if node and socket and images:
            access[node, socket] = (
                images["image"], tuple(item["name"] for item in spec.get("imagePullSecrets", [])),
            )
    return access


def reconcile_source_resources(
    kubectl: Kubectl, timeout: str, *, checkout: Path | None = None,
) -> None:
    """Reconcile a workstation's owned image pins while retaining completed and pending environments."""
    from foretoken.build.cluster.images import node_image_store
    from foretoken.platform.config import default_platform_config
    from foretoken.platform.helm import Helm

    if not has_server_binding(kubectl):
        return
    directory = cluster_state_directory(kubectl)
    directories = [directory, *(directory / "checkouts").glob("*")]
    # Pending and completed workspaces remain consumers without serving instances.
    states = {
        str(path): json.loads(path.read_text())
        for parent in directories if parent.is_dir()
        for name in ("build.json", "install.json")
        if (path := parent / name).is_file()
    }
    bindings = {
        state.get("build", {}).get("binding")
        for path, state in states.items()
        if (checkout is None or Path(path).parent == checkout)
        and state.get("build", {}).get("borrowed_images") is not True
    } - {None, ""}
    if not bindings:
        return
    producer_builds = [state.get("build", {}) for state in states.values()]
    config = default_platform_config()
    installed_path = directory / "install.json"
    installed = json.loads(installed_path.read_text()) if installed_path.is_file() else {}
    build = installed.get("build", {}) or next((build for build in producer_builds if build), {})
    tools = image_tools_image(build.get("arguments", {}), build.get("registry_mirrors", {}))
    secrets = tuple(build.get("pull_secrets", installed.get("runtime", {}).get("pull_secrets", [])))
    access = _source_image_access(kubectl.list_resources(
        ("pods",), config.namespace, label_selector=SOURCE_BUILD_POD_LABEL + "=true",
    ))
    sockets = {build.get("containerd_socket", "") for build in producer_builds} - {""}
    if not access and not sockets:
        return
    for node in kubectl.list_resources(("nodes",), ""):
        for socket in sockets:
            access.setdefault((node["metadata"]["name"], socket), (tools, secrets))
    history = Helm(config, kubectl).image_history()
    if history is None:
        return
    consumers = application_consumers(kubectl)
    retained = history | application_images(consumers)
    # Desired source selections protect their environments before materialization.
    if any(
        (obj.get("kind") == "InferenceDeployment" and obj.get("spec", {}).get("controllerSourceRevision"))
        or (obj.get("kind") in SERVICE_COMPONENTS and obj["metadata"].get("annotations", {}).get(SOURCE_REVISION))
        for obj in consumers
    ):
        try:
            retained.update(ApplicationFiles(kubectl, config.namespace).source_images(consumers))
        except DeploymentError as exc:
            logger.warning("Source image cleanup deferred: {}", exc)
            return
    retained.update(
        image for state in states.values()
        for image in state.get("build", {}).get("images", {}).values()
    )
    for (node, socket), (tools, secrets) in access.items():
        for binding in sorted(bindings):
            with node_image_store(
                kubectl, config.namespace, node, socket, tools, timeout,
                binding=binding, pull_secrets=secrets,
            ) as store:
                # Producer records authorize migrating an unlabeled source output.
                records = []
                for state in states.values():
                    recorded = state.get("build", {})
                    if recorded.get("binding") != binding or recorded.get("borrowed_images") is True:
                        continue
                    digests = recorded.get("digests", {}).get(node, {})
                    records.extend(
                        (image, digests[component]) for component, image in recorded.get("images", {}).items()
                        if image in retained and component in digests
                    )
                store.pin_records(records)
                store.release_except(retained)


def _remove_source_object(kubectl: Kubectl, document: dict[str, Any], timeout: str) -> None:
    """Retire one observed writer or cache UID without touching a replacement resource."""
    metadata = document["metadata"]
    namespace, name, uid = (metadata[key] for key in ("namespace", "name", "uid"))
    kind = document["kind"]
    current = kubectl.get_if_exists(kind, name, namespace)
    if current is None or current["metadata"]["uid"] != uid:
        return
    try:
        kubectl.delete_object(current, timeout, match_version=False)
    except DeploymentError:
        current = kubectl.get_if_exists(kind, name, namespace)
        if current is not None and current["metadata"]["uid"] == uid:
            raise
        return


@contextmanager
def source_uninstall(
    kubectl: Kubectl, timeout: str, *, namespace: str,
    origins: Iterable[dict[str, Any]], storage: Iterable[dict[str, Any]],
) -> Iterator[None]:
    """Retire origin-owned writers and caches, then release unused pins after platform removal.

    The platform lifecycle removes Helm resources inside this context. All cleanup
    identities and node access come from observed cluster resources, not local state.
    """
    from foretoken.build.cluster.images import node_image_store

    origin_owners = {("PersistentVolumeClaim", origin["name"], origin["uid"]) for origin in origins}
    if not origin_owners:
        yield
        return

    def owned(document: dict[str, Any], owners: set[tuple[str, str, str]]) -> bool:
        return any(
            (reference["kind"], reference["name"], reference["uid"]) in owners
            for reference in document["metadata"].get("ownerReferences", [])
        )

    claims = [
        claim for claim in kubectl.list_resources(
            ("persistentvolumeclaims",), namespace, label_selector=SOURCE_BUILD_CACHE_LABEL + "=true",
        )
        if owned(claim, origin_owners)
    ]
    cache_owners = {
        ("PersistentVolumeClaim", claim["metadata"]["name"], claim["metadata"]["uid"])
        for claim in claims
    }
    cache_bindings = {
        claim["metadata"]["name"]: claim["metadata"].get("labels", {}).get(SOURCE_BUILD_BINDING_LABEL)
        for claim in claims
    }
    workspaces = [
        workspace for workspace in kubectl.list_resources(("configmaps",), namespace, label_selector=WORKSPACE_LABEL)
        if owned(workspace, origin_owners)
    ]
    jobs = [
        job for job in kubectl.list_resources(
            ("jobs",), namespace, label_selector="foretoken.io/application-files=publisher",
        )
        if owned(job, origin_owners)
    ]
    writer_owners = origin_owners | cache_owners | {
        ("Job", job["metadata"]["name"], job["metadata"]["uid"]) for job in jobs
    }
    bindings = {binding for binding in cache_bindings.values() if binding}
    bindings.update(workspace["metadata"]["labels"][WORKSPACE_LABEL] for workspace in workspaces)
    bindings.update(job["metadata"]["labels"][APPLICATION_PUBLISHER_BINDING_LABEL] for job in jobs)
    storage = tuple(storage)
    storage_owners = {
        ("ConfigMap", document["metadata"]["name"], document["metadata"]["uid"])
        for document in storage if document["kind"] == "ConfigMap"
    }
    builders = []
    for pod in kubectl.list_resources(("pods",), namespace):
        labels = pod["metadata"].get("labels", {})
        # Older BuildKit daemons inherit ownership through their mounted cache.
        if owned(pod, writer_owners) or (
            labels.get(SOURCE_IMAGE_HELPER_LABEL) == "true"
            and labels.get(SOURCE_BUILD_BINDING_LABEL) in bindings and owned(pod, storage_owners)
        ) or (
            labels.get(SOURCE_BUILD_POD_LABEL) == "true"
            and labels.get(SOURCE_BUILD_BINDING_LABEL)
            and any(
                (name := volume.get("persistentVolumeClaim", {}).get("claimName")) in cache_bindings
                and cache_bindings[name] == labels[SOURCE_BUILD_BINDING_LABEL]
                for volume in pod["spec"].get("volumes", [])
            )
        ):
            builders.append(pod)
    bindings.update(
        binding for writer in [*jobs, *builders]
        for label in (SOURCE_BUILD_BINDING_LABEL, APPLICATION_PUBLISHER_BINDING_LABEL)
        if (binding := writer["metadata"].get("labels", {}).get(label))
    )
    access = _source_image_access(builders)
    if bindings:
        # The provisioner's installed helper configuration survives origin removal
        # and supplies the selected mirror and persistent pull-secret references.
        tools, secrets = image_tools_image({}), ()
        for document in storage:
            helper = document.get("data", {}).get("helperPod.yaml") if document["kind"] == "ConfigMap" else None
            if helper is not None:
                spec = yaml.safe_load(helper)["spec"]
                tools = spec["containers"][0]["image"]
                secrets = tuple(secret["name"] for secret in spec.get("imagePullSecrets", []))
                break
        context = kubectl.current_context()
        for node in kubectl.list_resources(("nodes",), ""):
            socket = node_containerd_socket(context, node)
            if socket:
                access.setdefault((node["metadata"]["name"], socket), (tools, secrets))
        # Build-owned login Secrets retire with their Pod; helpers reuse only
        # persistent references after the original writer has stopped.
        temporary_secrets = {pod["metadata"]["name"] for pod in builders}
        access = {
            key: (image, tuple(secret for secret in pull_secrets if secret not in temporary_secrets))
            for key, (image, pull_secrets) in access.items()
        }
    # Foreground deletion joins publisher children before any mounted PVC is released.
    for writer in [*jobs, *builders]:
        _remove_source_object(kubectl, writer, timeout)
    for claim in claims:
        metadata = claim["metadata"]
        current = kubectl.get_if_exists("pvc", metadata["name"], namespace)
        if current is None or current["metadata"]["uid"] != metadata["uid"]:
            continue
        if not owned(current, origin_owners):
            raise DeploymentError(f"compiler PVC {namespace}/{metadata['name']} changed ownership")
        consumers = [
            pod["metadata"]["name"] for pod in kubectl.list_resources(("pods",), namespace)
            if any(volume.get("persistentVolumeClaim", {}).get("claimName") == metadata["name"]
                   for volume in pod["spec"].get("volumes", []))
        ]
        if consumers:
            raise DeploymentError(
                f"compiler PVC {namespace}/{metadata['name']} is still used by: {', '.join(consumers)}"
            )
        _remove_source_object(kubectl, current, timeout)
    for workspace in workspaces:
        _remove_source_object(kubectl, workspace, timeout)
    try:
        yield
    finally:
        if bindings and access:
            # Observe after Helm removal, or preserve its still-live selections if
            # removal failed. Cleanup retains the original cancellation or error.
            with cleanup_commands(timeout=timeout_seconds(timeout)):
                native = kubectl.list_all_resources((
                    "pods", "jobs", "replicasets", "deployments", "statefulsets", "daemonsets", "cronjobs",
                ))
                retained = application_images(native) | application_images(application_consumers(kubectl))
                retained.update(application_images(
                    document["spec"]["jobTemplate"] for document in native if document["kind"] == "CronJob"
                ))
                for (node, socket), (tools, secrets) in access.items():
                    for binding in sorted(bindings):
                        with node_image_store(
                            kubectl, namespace, node, socket, tools, timeout,
                            binding=binding, pull_secrets=secrets,
                        ) as store:
                            store.release_except(retained)

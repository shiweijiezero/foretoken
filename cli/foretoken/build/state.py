# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own source bindings, operation locks, durable state and artifact retirement."""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

from foretoken.applications.files import (
    SERVICE_COMPONENTS,
    SOURCE_REVISION,
    ApplicationFiles,
    application_consumers,
    remove_application_jobs,
)
from foretoken.arguments import InstallCommand
from foretoken.build.cluster.builder import remove_build_pods
from foretoken.build.inputs import BuildInputs, engine_source_roots
from foretoken.build.cluster.images import (
    SOURCE_BUILD_BINDING_LABEL,
    SOURCE_BUILD_POD_LABEL,
    image_tools_image,
    node_containerd_socket,
)
from foretoken.build.cluster.storage import SOURCE_BUILD_CACHE_LABEL
from foretoken.command_execution import CommandExecution
from foretoken.deployment.manifest import ForetokenDeployment
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds

SOURCE_INSTALL_ANNOTATION = "foretoken.io/install-source"
SOURCE_BUILD_KEY = "source-build.json"


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
        *(_source_home().glob("*/retired/*/retired.json")),
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
        if state["context"]["server"] == server:
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
                print(f"Waiting for {description} (up to {timeout})", flush=True)
                reported = True
            time.sleep(min(0.2, remaining))


@contextmanager
def source_operation(
    kubectl: Kubectl, timeout: str, *, required: bool = False, shared: bool = False
) -> Iterator[None]:
    """Exclude platform changes while independent source deployments share the cluster.

    Install, delete and uninstall own cleanup under the exclusive lock. Deployments
    additionally lock their checkout state, so unrelated candidates can build together.
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
            if shared:
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
                remove_build_pods(
                    kubectl, timeout, binding=binding, preserve_ready=True
                )
            # Protect persisted source outputs before any compiler or workload can need them.
            reconcile_source_resources(kubectl, timeout)
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def checkout_state_directory(cluster: Path, root: Path, timeout: str) -> Path:
    """Allocate one stable publisher identity per checkout under a bounded registry lock."""
    checkouts = cluster / "checkouts"
    checkouts.mkdir(parents=True, exist_ok=True)
    with (checkouts / ".lock").open("a+") as lock:
        _acquire_source_lock(lock, timeout, "source checkout registration")
        for identity in checkouts.glob("*/checkout.json"):
            if (
                json.loads(identity.read_text())["root"] == str(root)
                and not (cluster / "retired" / identity.parent.name / "retired.json").is_file()
            ):
                return identity.parent
        directory = checkouts / uuid.uuid4().hex
        directory.mkdir()
        write_source_state(directory / "checkout.json", {
            "root": str(root), "services": [], "services_complete": True,
        })
        return directory


@contextmanager
def checkout_operation(directory: Path, kubectl: Kubectl, timeout: str) -> Iterator[None]:
    """Own a checkout's build outputs until their service selections have been submitted."""
    with (directory / ".lock").open("a+") as lock:
        _acquire_source_lock(lock, timeout, "this checkout's source operation")
        remove_application_jobs(kubectl, directory.name, timeout)
        remove_build_pods(kubectl, timeout, binding=directory.name, preserve_ready=True)
        yield


def record_source_deployment(directory: Path, deployment: ForetokenDeployment, kubectl: Kubectl) -> None:
    """Associate a live instance UID with its checkout before building or publishing applications."""
    path = directory / "checkout.json"
    identity = json.loads(path.read_text())
    services = [service for service in identity.get("services", []) if _source_consumer_exists(kubectl, service)]
    service = {"kind": "InferenceDeployment", "namespace": deployment.namespace, "name": deployment.name, "uid": deployment.uid}
    if service not in services:
        services.append(service)
    identity["services"] = services
    write_source_state(path, identity)


def _source_consumer_exists(kubectl: Kubectl, reference: dict[str, str]) -> bool:
    """Keep a recorded consumer only while its actual resource incarnation exists."""
    current = kubectl.get_if_exists(reference["kind"], reference["name"], reference["namespace"])
    return current is not None and ("uid" not in reference or current["metadata"]["uid"] == reference["uid"])


def release_source_deployment(
    kubectl: Kubectl, deployment: ForetokenDeployment, timeout: str,
) -> None:
    """Release this instance's checkout associations without requiring build permissions for ordinary members."""
    associated = any(
        reference.get("kind") == "InferenceDeployment"
        and reference["namespace"] == deployment.namespace and reference["name"] == deployment.name
        and (not deployment.uid or reference.get("uid") == deployment.uid)
        for path in _source_home().glob("*/checkouts/*/checkout.json")
        for reference in json.loads(path.read_text()).get("services", [])
    )
    retired = tuple(_source_home().glob("*/retired/*/retired.json"))
    pending_retirement = False
    if not associated and retired:
        context = context_identity(kubectl)
        pending_retirement = any(json.loads(path.read_text())["context"] == context for path in retired)
    if not associated and not pending_retirement:
        return
    with source_operation(kubectl, timeout, required=True):
        cluster = cluster_state_directory(kubectl)
        for path in (cluster / "checkouts").glob("*/checkout.json"):
            identity = json.loads(path.read_text())
            retained = [
                service for service in identity.get("services", [])
                if _source_consumer_exists(kubectl, service)
            ]
            if retained == identity.get("services", []):
                continue
            if retained or not identity.get("services_complete", False):
                identity["services"] = retained
                write_source_state(path, identity)
                if not retained:
                    print("Source build resources retained: earlier deployment ownership is unknown", flush=True)
            else:
                _retire_source_checkout(kubectl, path.parent)
        reconcile_source_resources(kubectl, timeout)


def _retire_source_checkout(kubectl: Kubectl, directory: Path) -> None:
    """Archive a checkout with no remaining deployments and transfer its resources to cleanup."""
    cluster = directory.parent.parent
    binding = directory.name
    record = cluster / "retired" / binding / "retired.json"
    if record.is_file():
        return
    identity = json.loads((directory / "checkout.json").read_text())
    states = [
        json.loads(path.read_text())
        for name in ("install.json", "build.json")
        if (path := directory / name).is_file()
    ]
    caches = [cache for state in states for cache in state.get("build", {}).get("caches", [])]
    # Include claims created just before an interrupted build recorded their UIDs.
    claims = kubectl.list_all_resources(
        ("persistentvolumeclaims",),
        label_selector=f"{SOURCE_BUILD_CACHE_LABEL}=true,{SOURCE_BUILD_BINDING_LABEL}={binding}",
    )
    for claim in claims:
        metadata = claim["metadata"]
        cache = {key: metadata[key] for key in ("namespace", "name", "uid")}
        if not any(all(value.get(key) == cache[key] for key in cache) for value in caches):
            caches.append({**cache, "binding": binding})
    if (caches and not states) or any(
        "inputs" not in state
        or (state["inputs"] is not None and not (directory / state["inputs"]).is_dir())
        for state in states
    ):
        raise DeploymentError("source cleanup needs the build's saved inputs; compiler caches were retained")
    evidence = _source_home().parent / "source-archives" / cluster.name / binding
    evidence.mkdir(parents=True, exist_ok=True)
    for abandoned in evidence.glob(".staging-*"):
        abandoned.unlink()
    # Preserve exact captured inputs, not a new scan of a checkout that may have changed.
    archive_path = evidence / (uuid.uuid4().hex + ".tar.gz")
    with tempfile.NamedTemporaryFile(prefix=".staging-", dir=evidence, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for path in directory.iterdir():
                if path.name.startswith("inputs-") or path.name in {"checkout.json", "install.json", "build.json"}:
                    archive.add(path, arcname=path.name)
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Source inputs archived locally: {evidence}", flush=True)
    record.parent.mkdir(parents=True, exist_ok=True)
    write_source_state(record, {
        "root": identity["root"],
        "context": context_identity(kubectl),
        "binding": binding,
        "caches": caches,
        "builds": [
            {key: value for key, value in state.get("build", {}).items()
             if key in {"binding", "containerd_socket", "arguments", "registry_mirrors", "pull_secrets"}}
            for state in states
        ],
    })
    # The durable retirement record excludes this binding from future source selections.
    # Its evidence lives outside forget_install's cluster-state deletion boundary.
    shutil.rmtree(directory)


def _remove_retired_caches(kubectl: Kubectl, record: dict[str, Any], timeout: str) -> bool:
    """Release recorded compiler UIDs after binding writers stop, deferring occupied volumes."""
    binding = record["binding"]
    remove_application_jobs(kubectl, binding, timeout)
    remove_build_pods(kubectl, timeout, binding=binding)
    complete = True
    for identity in record["caches"]:
        namespace, name, uid = (identity[key] for key in ("namespace", "name", "uid"))
        claim = kubectl.get_if_exists("pvc", name, namespace)
        if claim is None or claim["metadata"]["uid"] != uid:
            continue
        labels = claim["metadata"].get("labels", {})
        if labels.get(SOURCE_BUILD_CACHE_LABEL) != "true" or not (
            labels.get(SOURCE_BUILD_BINDING_LABEL) == binding
            or (identity.get("legacy") and SOURCE_BUILD_BINDING_LABEL not in labels)
        ):
            complete = False
            continue
        consumers = [
            pod for pod in kubectl.list_resources(("pods",), namespace)
            if any(volume.get("persistentVolumeClaim", {}).get("claimName") == name
                   for volume in pod["spec"].get("volumes", []))
        ]
        if consumers:
            complete = False
            continue
        try:
            kubectl.run(
                ["delete", "--raw", f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}", "-f", "-"],
                input_text=json.dumps({
                    "apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid},
                }),
            )
        except DeploymentError:
            current = kubectl.get_if_exists("pvc", name, namespace)
            if current is not None and current["metadata"]["uid"] == uid:
                raise
            continue
        kubectl.wait_deleted("persistentvolumeclaims", claim["metadata"], timeout)
    return complete


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
        inputs = BuildInputs.select(root, engines, previous, mooncake_version=mooncake_version)
        inputs.capture(destination, previous=previous)
        yield destination
    finally:
        if destination.exists():
            shutil.rmtree(destination)


@contextmanager
def local_candidates(directory: Path) -> Iterator[None]:
    """Retire artifacts outside committed or pending state under the owning source lock.

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
        for snapshot in directory.glob("inputs-*"):
            if snapshot.name not in retained:
                shutil.rmtree(snapshot)
        if (directory / "bundles").is_dir():
            shutil.rmtree(directory / "bundles")


def has_source_state(kubectl: Kubectl) -> bool:
    """Identify source builds for uninstall from local records or owned cluster caches."""
    if has_server_binding(kubectl):
        directory = cluster_state_directory(kubectl)
        if (
            any((directory / name).is_file() for name in ("install.json", "build.json"))
            or any((directory / "checkouts").glob("*/*.json"))
            or any((directory / "retired").glob("*/retired.json"))
        ):
            return True
    return bool(kubectl.list_all_resources(
        ("persistentvolumeclaims",), label_selector=SOURCE_BUILD_CACHE_LABEL + "=true"
    ))


def forget_install(kubectl: Kubectl) -> None:
    """Remove this cluster's workstation binding after platform uninstall, retaining remote data."""
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
    for name in ("install.json", "build.json"):
        path = directory / name
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
    build_state: dict[str, Any] | None = None,
) -> None:
    """Associate a successful editable installation with its checkout on this workstation."""
    if command.editable is None:
        return
    root = Path(command.editable).expanduser().resolve()
    directory = cluster_state_directory(kubectl)
    directory.mkdir(parents=True, exist_ok=True)
    settings = asdict(command)
    engines = engine_source_roots(command.engine_sources)
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
    if build_state is not None:
        # Developers on another workstation need build choices, not this machine's
        # paths, credentials, compiler volumes or captured installation history.
        recipe = {
            "registry": build_state["registry"],
            "ociRegistry": command.oci_registry,
            "baseImage": base_image,
            "runtimeBackend": build_state["runtime_backend"],
            "buildMetaXRuntime": build_state["build_metax_runtime"],
            "configuration": build_state["configuration"],
        }
        metadata = platform["metadata"]
        kubectl.run([
            "patch", "configmap", metadata["name"], "--namespace", metadata["namespace"],
            "--type=merge", "--patch", json.dumps({
                "metadata": {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]},
                "data": {SOURCE_BUILD_KEY: json.dumps(recipe)},
            }),
        ])
    path = directory / "install.json"
    previous = json.loads(path.read_text()) if path.is_file() else {}
    applications = (build_state or {}).get("applications", {})
    # Keep service-level source selections when installation adopts the same publication.
    # Removing them would change immutable Group specs on the next unchanged deploy.
    bundles = {
        component: bundle
        for component, bundle in previous.get("bundles", {}).items()
        if previous.get("root") == str(root)
        and bundle["revision"] == applications.get(component, {}).get("revision")
    }
    with local_candidates(directory):
        destination = directory / snapshot.name
        snapshot.rename(destination)
        write_source_state(
            path,
            {
                "root": str(root),
                "engines": engines,
                "build": build_state or {},
                "context": context_identity(kubectl),
                "inputs": destination.name,
                "platform_uid": platform["metadata"]["uid"],
                "base_image": base_image,
                "runtime": runtime_settings(platform),
                "command": settings,
                "bundles": bundles,
            },
        )
        (directory / "build.json").unlink(missing_ok=True)


def application_images(documents: Iterable[dict[str, Any]]) -> set[str]:
    """Read environment images selected by platform defaults, services and workload consumers."""
    images: set[str] = set()
    for document in documents:
        if document.get("kind") == "ConfigMap" and "controller-selection.json" in document.get("data", {}):
            selection = json.loads(document["data"]["controller-selection.json"])
            images.update(application_images([selection["template"]["controller"]]))
        if document.get("metadata", {}).get("labels", {}).get("foretoken.io/platform") == "configuration":
            runtime = runtime_settings(document)
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


def reconcile_source_resources(
    kubectl: Kubectl, timeout: str, *, uninstall: bool = False,
    checkout: Path | None = None,
) -> None:
    """Reconcile source image pins and finish retired bindings under source_operation.

    Checkout-scoped calls release only their own pins. Exclusive calls also collect
    retired compiler volumes and runtime files. Uninstall releases all Foretoken pins;
    the platform lifecycle owns its remaining storage and state cleanup.
    """
    from foretoken.build.cluster.images import node_image_store
    from foretoken.platform.config import default_platform_config
    from foretoken.platform.helm import Helm

    if not uninstall and not has_server_binding(kubectl):
        return
    directory = cluster_state_directory(kubectl)
    retired = {
        path: json.loads(path.read_text())
        for path in (directory / "retired").glob("*/retired.json")
    }
    retired_bindings = {record["binding"] for record in retired.values()}
    directories = [directory, *(directory / "checkouts").glob("*")]
    # Reference collection is cluster-wide even when one checkout owns this operation.
    # Retired state is producer evidence, never a reason to keep all its old outputs.
    states = {
        str(path): json.loads(path.read_text())
        for parent in directories if parent.is_dir() and parent.name not in retired_bindings
        for name in ("build.json", "install.json")
        if (path := parent / name).is_file()
    }
    bindings = {
        state.get("build", {}).get("binding")
        for path, state in states.items()
        if (checkout is None or Path(path).parent == checkout)
        and state.get("build", {}).get("borrowed_images") is not True
    } - {None, ""}
    completed_bindings = set()
    if checkout is None and not uninstall:
        for path, record in retired.items():
            if _remove_retired_caches(kubectl, record, timeout):
                completed_bindings.add(record["binding"])
            abandoned = directory / "checkouts" / record["binding"]
            if abandoned.is_dir():
                shutil.rmtree(abandoned)
        bindings.update(retired_bindings)
    if not uninstall and not bindings:
        return
    producer_builds = [state.get("build", {}) for state in states.values()]
    if checkout is None:
        producer_builds.extend(build for record in retired.values() for build in record["builds"])
    config = default_platform_config()
    if checkout is None and not uninstall and retired:
        try:
            origin = ApplicationFiles(kubectl, config.namespace)
            references = origin.references(Helm(config, kubectl).application_history())
            if references is None:
                completed_bindings.clear()
            else:
                # Saved candidates may need an application before their next service submission.
                references.update(
                    revision
                    for state in states.values()
                    for bundles in (state.get("bundles", {}), state.get("build", {}).get("applications", {}))
                    for bundle in bundles.values()
                    for key in ("revision", "previous")
                    if (revision := bundle.get(key))
                )
                origin.prepare(timeout)
                for record in retired.values():
                    if not origin.retire_source(record["binding"], references, timeout=timeout):
                        completed_bindings.discard(record["binding"])
        except DeploymentError as error:
            completed_bindings.clear()
            print(f"Source application cleanup deferred: {error}", flush=True)
    installed_path = directory / "install.json"
    installed = json.loads(installed_path.read_text()) if installed_path.is_file() else {}
    build = installed.get("build", {}) or next(
        (build for build in producer_builds if build), {}
    )
    tools = image_tools_image(
        build.get("arguments", {}), build.get("registry_mirrors", {})
    )
    secrets = tuple(
        installed.get("runtime", {}).get("pull_secrets", build.get("pull_secrets", []))
    )
    # Builders already describe node runtime access. Use those cluster-owned
    # mounts during uninstall even when invoked from a different workstation.
    access: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
    for pod in kubectl.list_resources(
        ("pods",),
        config.namespace,
        label_selector=SOURCE_BUILD_POD_LABEL + "=true",
    ):
        node = pod["spec"].get("nodeName")
        socket = next(
            (
                volume["hostPath"]["path"]
                for volume in pod["spec"].get("volumes", [])
                if volume["name"] == "runtime-socket"
            ),
            None,
        )
        images = next(
            (
                container
                for container in pod["spec"]["containers"]
                if container["name"] == "images"
            ),
            None,
        )
        if node and socket and images:
            access[node, socket] = (
                images["image"],
                tuple(item["name"] for item in pod["spec"].get("imagePullSecrets", [])),
            )
    sockets = {build.get("containerd_socket", "") for build in producer_builds} - {""}
    # Registry-only builds have no node image store to reconcile. Existing
    # builders and pending state still retain access after a delivery-mode change.
    if not uninstall and not access and not sockets:
        for path, record in retired.items():
            if record["binding"] in completed_bindings:
                shutil.rmtree(path.parent)
        return
    nodes = kubectl.list_resources(("nodes",), "")
    if uninstall and not sockets:
        context = kubectl.current_context()
        for node in nodes:
            socket = node_containerd_socket(context, node)
            if socket:
                access.setdefault((node["metadata"]["name"], socket), (tools, secrets))
    for socket in sockets:
        for node in nodes:
            access.setdefault((node["metadata"]["name"], socket), (tools, secrets))
    retained: set[str] = set()
    if not uninstall:
        history = Helm(config, kubectl).image_history()
        if history is None:
            return
        retained.update(history)
        consumers = application_consumers(kubectl)
        retained.update(application_images(consumers))
        # Desired source selections protect their environments before a controller
        # or its serving status has materialized the selected image.
        if any(
            (obj.get("kind") == "InferenceDeployment" and obj.get("spec", {}).get("controllerSourceRevision"))
            or (obj.get("kind") in SERVICE_COMPONENTS and obj["metadata"].get("annotations", {}).get(SOURCE_REVISION))
            for obj in consumers
        ):
            try:
                retained.update(ApplicationFiles(kubectl, config.namespace).source_images(consumers))
            except DeploymentError as exc:
                # An unavailable publication cannot prove its image unreferenced.
                # Keep pins so platform repair and source deployment can still proceed.
                print(f"Source image cleanup deferred: {exc}", flush=True)
                return
        retained.update(
            image
            for state in states.values()
            for image in state.get("build", {}).get("images", {}).values()
        )
    for (node, socket), (tools, secrets) in access.items():
        for binding in [""] if uninstall else sorted(bindings):
            with node_image_store(
                kubectl,
                config.namespace,
                node,
                socket,
                tools,
                timeout,
                binding=binding,
                pull_secrets=secrets,
            ) as store:
                if not uninstall:
                    # Persisted producer records authorize migrating an unlabeled source output.
                    records = []
                    for state in states.values():
                        recorded = state.get("build", {})
                        if (
                            recorded.get("binding") != binding
                            or recorded.get("borrowed_images") is True
                        ):
                            continue
                        digests = recorded.get("digests", {}).get(node, {})
                        for component, image in recorded.get("images", {}).items():
                            if image in retained and component in digests:
                                records.append((image, digests[component]))
                    store.pin_records(records)
                if store.release_except(retained):
                    completed_bindings.discard(binding)
    for path, record in retired.items():
        if record["binding"] in completed_bindings:
            shutil.rmtree(path.parent)


def remove_build_caches(kubectl: Kubectl, timeout: str) -> None:
    """Release managed build Pods before their compiler volumes, leaving model caches intact."""
    remove_build_pods(kubectl, timeout)
    kubectl.run(
        [
            "delete",
            "pvc",
            "--all-namespaces",
            "-l",
            SOURCE_BUILD_CACHE_LABEL + "=true",
            "--ignore-not-found",
            "--timeout=" + timeout,
        ]
    )

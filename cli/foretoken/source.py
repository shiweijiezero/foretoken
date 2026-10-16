# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own source installation bindings, build inputs and platform application preparation."""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from foretoken.application_files import (
    SERVICE_COMPONENTS,
    SOURCE_REVISION,
    ApplicationFiles,
    application_consumers,
    remove_application_jobs,
)
from foretoken.arguments import InstallCommand
from foretoken.build_environments import BuildEnvironments
from foretoken.build_storage import (
    SOURCE_BUILD_PROVISIONER_IMAGE,
    SOURCE_BUILD_STORAGE_CLASS,
    ensure_build_storage,
    superseded_build_volume,
)
from foretoken.cluster_build import (
    BuildCancellation,
    ClusterBuilder,
    find_build_cache,
    registry_credentials,
    remove_build_pods,
)
from foretoken.engine_environment import (
    engine_build_arguments,
    engine_cache_scope,
    resolve_vllm_base_image,
)
from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError, ForetokenDeployment
from foretoken.network_sources import select_build_sources
from foretoken.node_images import SOURCE_BUILD_BINDING_LABEL, SOURCE_BUILD_POD_LABEL

_BUILD_CACHE_LABEL = "inference.foretoken.io/source-build-cache"
_INSTALL_SOURCE = "foretoken.io/install-source"
_SOURCE_METADATA = ".foretoken-sources.json"
_SNAPSHOT_METADATA = frozenset({"engine/manifest.json", "engine/deleted.json", _SOURCE_METADATA})
# Workstation build entrypoints and explicitly applied values are not cluster build inputs.
_NON_BUILD_INPUTS = frozenset(
    {"Makefile", "deploy/dev-build", "deploy/platform-values.yaml"}
)


def _source_home() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "foretoken"
        / "source"
    )


def _context_identity(kubectl: Kubectl) -> dict[str, str]:
    """Read connection identity locally without adding API permissions to release deploys."""
    config = json.loads(
        kubectl.run(["config", "view", "--minify", "-o", "json"]).stdout
    )
    return {"server": config["clusters"][0]["cluster"]["server"]}


def _state_directory(kubectl: Kubectl) -> Path:
    """Keep workstation paths local and bind them to the actual cluster identity."""
    cluster = kubectl.get("namespace", "kube-system")["metadata"]["uid"]
    return _source_home() / cluster


def _has_server_binding(kubectl: Kubectl) -> bool:
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
    server = _context_identity(kubectl)["server"]
    for path in bindings:
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            # An uninstall may retire a binding before this operation acquires its lock.
            continue
        if state["context"]["server"] == server:
            return True
    return False


@contextmanager
def source_operation(
    kubectl: Kubectl, timeout: str, *, installing: bool = False, shared: bool = False
) -> Iterator[None]:
    """Exclude platform changes while independent source deployments share the cluster.

    Install, delete and uninstall own cleanup under the exclusive lock. Deployments
    additionally lock their checkout state, so unrelated candidates can build together.
    """
    if not installing and not _has_server_binding(kubectl):
        yield
        return
    # POSIX locking is needed only for source operations, not other CLI commands.
    import fcntl

    directory = _state_directory(kubectl)
    locks = _source_home() / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / directory.name).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
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


def source_checkout(explicit: str | None = None) -> Path:
    """Resolve the deployment's checkout without borrowing another worktree's source."""
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
    else:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise DeploymentError(
                "source deployment requires a Foretoken checkout; run from that checkout or pass --editable PATH"
            )
        root = Path(result.stdout.strip()).resolve()
    if not (root / "deploy/charts/foretoken/Chart.yaml").is_file():
        raise DeploymentError(f"not a Foretoken source checkout: {root}")
    return root


def checkout_state_directory(cluster: Path, root: Path) -> Path:
    """Allocate one stable publisher identity per checkout under a short registry lock."""
    import fcntl

    checkouts = cluster / "checkouts"
    checkouts.mkdir(parents=True, exist_ok=True)
    with (checkouts / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for identity in checkouts.glob("*/checkout.json"):
            if (
                json.loads(identity.read_text())["root"] == str(root)
                and not (cluster / "retired" / identity.parent.name / "retired.json").is_file()
            ):
                return identity.parent
        directory = checkouts / uuid.uuid4().hex
        directory.mkdir()
        _write_json(directory / "checkout.json", {
            "root": str(root), "services": [], "services_complete": True,
        })
        return directory


@contextmanager
def checkout_operation(directory: Path, kubectl: Kubectl, timeout: str) -> Iterator[None]:
    """Own a checkout's build outputs until their service selections have been submitted."""
    import fcntl

    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        remove_application_jobs(kubectl, directory.name, timeout)
        remove_build_pods(kubectl, timeout, binding=directory.name, preserve_ready=True)
        yield


def record_source_deployment(directory: Path, deployment: ForetokenDeployment) -> None:
    """Associate a checkout with intended services before building, including failed submissions."""
    path = directory / "checkout.json"
    identity = json.loads(path.read_text())
    services = identity.setdefault("services", [])
    for ref in deployment.service_refs():
        service = {"kind": ref.kind, "namespace": ref.namespace, "name": ref.name}
        if service not in services:
            services.append(service)
    _write_json(path, identity)


def release_source_deployment(
    kubectl: Kubectl, deployment: ForetokenDeployment, timeout: str,
) -> None:
    """Release a deleted deployment's source associations and clean unused builds under the source lock."""
    if not _has_server_binding(kubectl):
        return
    cluster = _state_directory(kubectl)
    services = [
        {"kind": ref.kind, "namespace": ref.namespace, "name": ref.name}
        for ref in deployment.service_refs()
    ]
    namespaces = {
        obj["metadata"]["name"] for obj in deployment.objects if obj.get("kind") == "Namespace"
    }
    for path in (cluster / "checkouts").glob("*/checkout.json"):
        identity = json.loads(path.read_text())
        retained = [
            service for service in identity.get("services", [])
            if service not in services and service["namespace"] not in namespaces
        ]
        if retained == identity.get("services", []):
            continue
        if retained or not identity.get("services_complete", False):
            identity["services"] = retained
            _write_json(path, identity)
            if not retained:
                print("Source build resources retained: earlier deployment ownership is unknown", flush=True)
        else:
            # Keep the final association until its recovery archive and retirement
            # record are durable, so repeating delete can finish interrupted cleanup.
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
        label_selector=f"{_BUILD_CACHE_LABEL}=true,{SOURCE_BUILD_BINDING_LABEL}={binding}",
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
    _write_json(record, {
        "root": identity["root"],
        "context": _context_identity(kubectl),
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
        if labels.get(_BUILD_CACHE_LABEL) != "true" or not (
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


def engine_source_roots(sources: tuple[str, ...]) -> dict[str, str]:
    """Resolve explicitly selected runtime checkouts, independently of Rust's pinned dependency."""
    roots = {}
    for source in sources:
        name, separator, value = source.partition("=")
        if not separator:
            name, value = "vllm", source
        if name not in {"vllm", "vllm-metax"} or name in roots:
            raise DeploymentError(
                "--engine-source accepts one vllm checkout and one optional vllm-metax checkout"
            )
        path = Path(value).expanduser().resolve()
        package = path / name.replace("-", "_")
        if not package.is_dir() or not (path / "setup.py").is_file():
            raise DeploymentError(f"engine source is not a {name} checkout: {path}")
        roots[name] = str(path)
    if roots and "vllm" not in roots:
        raise DeploymentError(
            "--engine-source vllm-metax=PATH also requires the corresponding --engine-source vllm=PATH"
        )
    return roots


def _inputs(root: Path, engines: dict[str, str] | None = None) -> dict[str, Path]:
    """Select build inputs using Git's tracked and non-ignored source files."""
    command = [
        "git",
        "-C",
        str(root),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise DeploymentError(result.stderr.decode().strip())
    files = {}
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = root / name
        if not name.startswith(
            ("data-plane/", "control-plane/", "deploy/")
        ) and name not in {"Makefile", "LICENSE", ".dockerignore", ".gitmodules"}:
            continue
        if name in _NON_BUILD_INPUTS:
            continue
        if path.is_file() and path.suffix not in {".md", ".png", ".svg"}:
            files[name] = path
    # Rust dependencies use the pinned submodule. Its own ignore rules exclude build caches.
    upstream = root / "data-plane/third_party/vllm"
    if (upstream / ".git").exists():
        result = subprocess.run(
            [
                "git",
                "-C",
                str(upstream),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "rust",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise DeploymentError(result.stderr.decode().strip())
        for raw in result.stdout.split(b"\0"):
            if raw:
                name = "data-plane/third_party/vllm/" + os.fsdecode(raw)
                if (root / name).is_file():
                    files[name] = root / name
    preparation = upstream / ".foretoken-patches.json"
    if preparation.is_file():
        files[str(preparation.relative_to(root))] = preparation
    mooncake = root / "third_party/mooncake"
    if (mooncake / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(mooncake), "ls-files", "--recurse-submodules", "-z"],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise DeploymentError(result.stderr.decode().strip())
        for raw in result.stdout.split(b"\0"):
            if raw:
                name = "third_party/mooncake/" + os.fsdecode(raw)
                if (root / name).is_file():
                    files[name] = root / name
    for engine, checkout in (engines or {}).items():
        result = subprocess.run(
            [
                "git",
                "-C",
                checkout,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise DeploymentError(result.stderr.decode().strip())
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            name = os.fsdecode(raw)
            path = Path(checkout) / name
            if path.is_file():
                files[f"engine/{engine}/{name}"] = path
    return files


def _snapshot(
    files: dict[str, Path],
    destination: Path,
    engines: dict[str, str] | None = None,
    previous: Path | None = None,
    unchanged: set[str] | None = None,
    *,
    root: Path,
) -> None:
    """Save build inputs and their checkout provenance before compilation begins."""
    destination.mkdir(parents=True)
    for name, source in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        prior = previous / name if previous is not None else None
        if prior is not None and unchanged is not None and name in unchanged:
            os.link(prior, target)
        else:
            shutil.copy2(source, target)
    if engines:
        manifest = {}
        deleted = {}
        for engine, checkout in engines.items():
            prefix = f"engine/{engine}/"
            manifest[engine] = {
                "files": sorted(
                    name.removeprefix(prefix)
                    for name in files
                    if name.startswith(prefix)
                )
            }
            result = subprocess.run(
                ["git", "-C", checkout, "ls-files", "--deleted", "-z"],
                capture_output=True,
                check=True,
            )
            deleted[engine] = [
                os.fsdecode(name) for name in result.stdout.split(b"\0") if name
            ]
        (destination / "engine").mkdir(exist_ok=True)
        _write_json(destination / "engine/manifest.json", manifest)
        _write_json(destination / "engine/deleted.json", deleted)
    _write_json(destination / _SOURCE_METADATA, {
        "foretoken": _checkout_source(root),
        "engines": {name: _checkout_source(Path(checkout)) for name, checkout in (engines or {}).items()},
    })


def _checkout_source(root: Path) -> dict[str, Any] | None:
    """Record a build checkout's Git state without retaining local paths or remote credentials."""
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True, check=False)
    if result.returncode:
        return None
    changed = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], text=True)
    return {"commit": result.stdout.strip(), "dirty": bool(changed)}


def build_sources(snapshot: Path, component: str) -> dict[str, Any] | None:
    """Read captured provenance for the component being published, including bound model engines."""
    metadata = snapshot / _SOURCE_METADATA
    if not metadata.is_file():
        return None
    sources = json.loads(metadata.read_text())
    if component != "model-server":
        sources.pop("engines", None)
    return sources


def _write_json(path: Path, value: object) -> None:
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
    root: Path, engines: dict[str, str] | None = None
) -> Iterator[Path]:
    """Capture candidate inputs until their source owner adopts the snapshot into saved state."""
    destination = _source_home() / "builds" / ("inputs-" + uuid.uuid4().hex)
    try:
        _snapshot(_inputs(root, engines), destination, engines, root=root)
        yield destination
    finally:
        if destination.exists():
            shutil.rmtree(destination)


@contextmanager
def _local_candidates(directory: Path) -> Iterator[None]:
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
    if _has_server_binding(kubectl):
        directory = _state_directory(kubectl)
        if (
            any((directory / name).is_file() for name in ("install.json", "build.json"))
            or any((directory / "checkouts").glob("*/*.json"))
            or any((directory / "retired").glob("*/retired.json"))
        ):
            return True
    return bool(kubectl.list_all_resources(
        ("persistentvolumeclaims",), label_selector=_BUILD_CACHE_LABEL + "=true"
    ))


def forget_install(kubectl: Kubectl) -> None:
    """Remove this cluster's workstation binding after platform uninstall, retaining remote data."""
    if _has_server_binding(kubectl):
        directory = _state_directory(kubectl)
        if directory.exists():
            shutil.rmtree(directory)


def validate_build_inputs(
    root: Path, snapshot: Path, engines: dict[str, str] | None = None
) -> None:
    """Reject a build whose checkout changed before its image or bundle was selected."""
    current = _inputs(root, engines)
    previous = {
        str(path.relative_to(snapshot))
        for path in snapshot.rglob("*")
        if path.is_file()
        and str(path.relative_to(snapshot)) not in _SNAPSHOT_METADATA | _NON_BUILD_INPUTS
    }
    if previous != current.keys() or any(
        not filecmp.cmp(snapshot / name, path, shallow=False)
        or (snapshot / name).stat().st_mode != path.stat().st_mode
        for name, path in current.items()
    ):
        raise DeploymentError(
            "source changed while preparing artifacts; rerun the source operation"
        )


def snapshot_versions(
    snapshot: Path,
    previous: Path | None,
    versions: dict[str, str],
    changed: set[str] | None = None,
) -> dict[str, str]:
    """Assign a new revision only to changed input paths for cluster-side delta transfer."""
    revision = snapshot.name.removeprefix("inputs-")
    result = {}
    for path in snapshot.rglob("*"):
        if not path.is_file():
            continue
        name = str(path.relative_to(snapshot))
        old = previous / name if previous else None
        unchanged = (
            name in versions and name not in changed
            if changed is not None and name not in _SNAPSHOT_METADATA
            else (
                name in versions
                and old is not None
                and old.is_file()
                and old.stat().st_mode == path.stat().st_mode
                and filecmp.cmp(old, path, shallow=False)
            )
        )
        result[name] = versions[name] if unchanged else revision
    return result


def _runtime_settings(platform: dict[str, Any]) -> dict[str, Any]:
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
    directory = _state_directory(kubectl)
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
    directory = _state_directory(kubectl)
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

    platform = Helm(default_platform_config(command.oci_registry)).platform_configuration(kubectl)
    if platform["metadata"].get("annotations", {}).get(_INSTALL_SOURCE) != "source":
        raise DeploymentError("expected a source-installed Foretoken platform")
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
    with _local_candidates(directory):
        destination = directory / snapshot.name
        snapshot.rename(destination)
        _write_json(
            path,
            {
                "root": str(root),
                "engines": engines,
                "build": build_state or {},
                "context": _context_identity(kubectl),
                "inputs": destination.name,
                "platform_uid": platform["metadata"]["uid"],
                "base_image": base_image,
                "runtime": _runtime_settings(platform),
                "command": settings,
                "bundles": bundles,
            },
        )
        (directory / "build.json").unlink(missing_ok=True)


@dataclass(frozen=True)
class SourceImages:
    """Built environments, application references and inputs for installation or service deployment."""

    source_root: Path
    image_mode: str
    control_plane: str
    frontend: str
    model_server: str
    inputs: Path
    build_state: dict[str, Any]
    applications: dict[str, str]


def pinned_rust_revision(root: Path) -> str:
    """Read the repository's dependency identity without downloading its source to the client."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--stage",
            "--",
            "data-plane/third_party/vllm",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    fields = result.stdout.split()
    if result.returncode or len(fields) != 4 or fields[0] != "160000":
        raise DeploymentError(
            "source installation requires the recorded vLLM Git submodule"
        )
    return fields[1]


def image_tools_image(
    arguments: dict[str, str],
    registry_mirrors: dict[str, list[str]] | None = None,
) -> str:
    """Select Bash and GNU tar for node image operations using the build's Docker mirror."""
    automatic = (registry_mirrors or {}).get("docker.io", ["docker.io"])[0]
    registry = arguments.get("BASE_IMAGE_REGISTRY", automatic).rstrip("/")
    return f"{registry}/library/debian:bookworm-slim"


def build_configuration(
    root: Path, values: Sequence[dict[str, Any]], image_registry: str
) -> dict[str, str]:
    """Resolve compiler storage and image choices from the chart's authoritative defaults."""
    defaults = yaml.safe_load(
        (root / "deploy/charts/foretoken/values.yaml").read_text()
    )
    configuration = dict(defaults["development"]["build"])
    explicit_image = False
    for value in values:
        overrides = (value.get("development") or {}).get("build") or {}
        explicit_image = explicit_image or "image" in overrides
        configuration.update(overrides)
    if image_registry and not explicit_image:
        configuration["image"] = (
            image_registry.rstrip("/") + "/" + configuration["image"].split("/", 1)[1]
        )
    return configuration


def build_arguments(environment: dict[str, str]) -> dict[str, str]:
    """Map source mirror choices explicitly onto the existing image build interfaces."""
    result = {
        name: environment[name]
        for name in (
            "GOPROXY",
            "GOSUMDB",
            "FORETOKEN_GITHUB_MIRROR",
            "FORETOKEN_CARGO_REGISTRY",
            "CARGO_NET_GIT_FETCH_WITH_CLI",
            "UV_DEFAULT_INDEX",
            "UV_EXTRA_INDEX_URL",
            "UV_IMAGE",
            "METAX_SDK_IMAGE",
            "MACA_PATH",
            "UV_PYTHON",
            "FORETOKEN_VLLM_PYTHON",
            "BUILD_JOBS",
        )
        if environment.get(name)
    }
    if environment.get("TORCH_CUDA_ARCH_LIST"):
        result["TARGET_CUDA_ARCH_LIST"] = environment["TORCH_CUDA_ARCH_LIST"]
    registry = environment.get("FORETOKEN_OCI_REGISTRY", "")
    docker = environment.get("FORETOKEN_DOCKER_IO_REGISTRY", registry).rstrip("/")
    ghcr = environment.get("FORETOKEN_GHCR_REGISTRY", registry).rstrip("/")
    gcr = environment.get("FORETOKEN_GCR_REGISTRY", registry).rstrip("/")
    if docker:
        result.update(
            BASE_IMAGE_REGISTRY=docker,
            GO_IMAGE_REGISTRY=docker,
            BUILDKIT_SYNTAX_IMAGE=f"{docker}/docker/dockerfile:1",
        )
    if ghcr:
        result["UV_IMAGE_REGISTRY"] = ghcr
    if gcr:
        result["DISTROLESS_IMAGE_REGISTRY"] = gcr
    return result


def prepare_build_storage(
    kubectl: Kubectl,
    namespace: str,
    arguments: dict[str, str],
    registry_mirrors: dict[str, list[str]],
    pull_secrets: tuple[str, ...],
    timeout: str,
) -> None:
    """Prepare node-local execution storage for installation, preserving artifact storage intent."""
    automatic = registry_mirrors.get("docker.io", ["docker.io"])[0]
    registry = arguments.get("BASE_IMAGE_REGISTRY", automatic).rstrip("/")
    image = registry + "/" + SOURCE_BUILD_PROVISIONER_IMAGE.split("/", 1)[1]
    ensure_build_storage(
        kubectl, namespace, image_tools_image(arguments, registry_mirrors),
        image, pull_secrets, timeout,
    )


def ensure_build_cache(
    kubectl: Kubectl,
    namespace: str,
    name: str,
    configuration: dict[str, str],
    *,
    binding: str,
    timeout: str,
    recorded_caches: Sequence[dict[str, Any]] = (),
    owner: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Prepare this binding's compiler volume and return its proven identity for retirement.

    Superseded managed storage is reclaimed before rebuilding; legacy ownership
    requires a binding-owned builder or its previously recorded claim UID.
    """
    if not kubectl.exists("namespace", namespace):
        kubectl.run(["create", "namespace", namespace])
    storage_class = configuration["storageClassName"] or SOURCE_BUILD_STORAGE_CLASS
    existing = kubectl.get_if_exists("pvc", name, namespace)
    owners = [owner] if owner is not None else []
    if existing is not None:
        if existing["metadata"].get("labels", {}).get(_BUILD_CACHE_LABEL) != "true":
            raise DeploymentError(f"{namespace}/{name} is not a Foretoken build cache")
        claim_binding = existing["metadata"].get("labels", {}).get(SOURCE_BUILD_BINDING_LABEL)
        if claim_binding not in {None, binding}:
            raise DeploymentError(f"{namespace}/{name} belongs to another source binding")
        actual_class = existing["spec"].get("storageClassName", "")
        if configuration["storageClassName"] and actual_class != configuration["storageClassName"]:
            raise DeploymentError(
                f"compiler cache {namespace}/{name} uses StorageClass {actual_class!r}; "
                "an existing compiler volume cannot change its storage class"
            )
        storage_class = actual_class
        recorded_legacy = any(
            identity.get("legacy") is True and identity.get("binding") == binding
            and all(identity.get(key) == existing["metadata"][key] for key in ("namespace", "name", "uid"))
            for identity in recorded_caches
        )
        volume = superseded_build_volume(kubectl, existing)
        if volume is not None:
            consumers = [
                pod for pod in kubectl.list_resources(("pods",), namespace)
                if any(item.get("persistentVolumeClaim", {}).get("claimName") == name for item in pod["spec"].get("volumes", []))
            ]
            for pod in consumers:
                labels = pod["metadata"].get("labels", {})
                if labels.get(SOURCE_BUILD_POD_LABEL) != "true" or labels.get(SOURCE_BUILD_BINDING_LABEL) != binding:
                    raise DeploymentError(f"compiler cache {namespace}/{name} is still used by {pod['metadata']['name']}; finish that operation before updating storage")
            if claim_binding is None and not consumers and not recorded_legacy:
                raise DeploymentError(f"cannot replace legacy compiler cache {namespace}/{name}: its source binding is unknown")
            print(f"Replacing compiler cache {namespace}/{name} on the selected local storage; published applications are retained", flush=True)
            owners = existing["metadata"].get("ownerReferences", owners)
            for resource, obj in [*(("pods", pod) for pod in consumers), ("persistentvolumeclaims", existing)]:
                metadata = obj["metadata"]
                kubectl.run(
                    ["delete", "--raw", f"/api/v1/namespaces/{namespace}/{resource}/{metadata['name']}", "-f", "-"],
                    input_text=json.dumps({"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": metadata["uid"]}}),
                )
                kubectl.wait_deleted(resource, metadata, timeout)
            kubectl.wait_deleted("persistentvolume", {**volume["metadata"], "namespace": ""}, timeout)
            existing = None
    if (
        storage_class == SOURCE_BUILD_STORAGE_CLASS
        and (existing is None or existing.get("status", {}).get("phase") != "Bound")
        and not kubectl.exists("storageclass", storage_class)
    ):
        raise DeploymentError(
            "source build storage is not installed; update the source platform "
            "with `foretoken install -e PATH` before deploying"
        )
    if existing is not None:
        spec = {"resources": {"requests": {"storage": configuration["storageSize"]}}}
        if any(existing["spec"].get(key) != value for key, value in spec.items()):
            kubectl.run(
                [
                    "patch",
                    "pvc",
                    name,
                    "-n",
                    namespace,
                    "--type=merge",
                    "-p",
                    json.dumps({"spec": spec}),
                ]
            )
        if claim_binding is None and not recorded_legacy:
            builders = kubectl.list_resources(
                ("pods",), namespace,
                label_selector=f"{SOURCE_BUILD_POD_LABEL}=true,{SOURCE_BUILD_BINDING_LABEL}={binding}",
            )
            if not any(
                volume["name"] == "cache"
                and volume.get("persistentVolumeClaim", {}).get("claimName") == name
                for pod in builders
                for volume in pod["spec"].get("volumes", [])
            ):
                return None
        return {
            "namespace": namespace, "name": name, "uid": existing["metadata"]["uid"],
            "binding": binding,
            **({"legacy": True} if claim_binding is None else {}),
        }
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": configuration["storageSize"]}},
        "storageClassName": storage_class,
    }
    created = kubectl.run(
        ["create", "-f", "-", "-o", "json"],
        input_text=yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {
                        _BUILD_CACHE_LABEL: "true",
                        SOURCE_BUILD_BINDING_LABEL: binding,
                    },
                    **({"ownerReferences": owners} if owners else {}),
                },
                "spec": spec,
            }
        ),
    )
    metadata = json.loads(created.stdout)["metadata"]
    return {"namespace": namespace, "name": name, "uid": metadata["uid"], "binding": binding}


def ensure_build_volumes(
    kubectl: Kubectl,
    namespace: str,
    name: str,
    configuration: dict[str, str],
    *,
    binding: str,
    timeout: str,
    record_identity: Callable[[dict[str, Any]], None],
    recorded_caches: Sequence[dict[str, Any]] = (),
    owner: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Prepare artifact and optional local execution PVCs for source build callers.

    The source state owner records each UID before another PVC is created, so a
    failed pair remains recoverable. Default local storage uses a single volume;
    explicitly selected artifact storage receives a separate local execution PVC.
    """
    artifact = ensure_build_cache(
        kubectl, namespace, name, configuration,
        binding=binding, timeout=timeout, recorded_caches=recorded_caches, owner=owner,
    )
    if artifact is not None:
        record_identity(artifact)
    # An omitted override preserves an existing volume's class, including NFS.
    current = kubectl.get("pvc", name, namespace)
    if current["spec"].get("storageClassName", "") in ("", SOURCE_BUILD_STORAGE_CLASS):
        return artifact, None
    if artifact is None:
        raise DeploymentError(
            f"cannot prepare execution storage: artifact PVC {namespace}/{name} has no proven source binding"
        )
    if artifact.get("legacy"):
        metadata = current["metadata"]
        labels = metadata.get("labels", {})
        if metadata["uid"] != artifact["uid"] or labels.get(_BUILD_CACHE_LABEL) != "true" or labels.get(SOURCE_BUILD_BINDING_LABEL) not in (None, binding):
            raise DeploymentError(f"artifact PVC {namespace}/{name} changed during ownership transfer")
        updated = json.loads(kubectl.run([
            "patch", "pvc", name, "-n", namespace, "--type=json", "-o", "json", "-p", json.dumps([
                {"op": "test", "path": "/metadata/uid", "value": artifact["uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
                {"op": "add", "path": "/metadata/labels/" + SOURCE_BUILD_BINDING_LABEL.replace("/", "~1"), "value": binding},
            ]),
        ]).stdout)["metadata"]
        artifact = {key: updated[key] for key in ("namespace", "name", "uid")}
        artifact["binding"] = updated["labels"][SOURCE_BUILD_BINDING_LABEL]
        record_identity(artifact)
    execution_name = "foretoken-execution-" + artifact["uid"][:20]
    execution = ensure_build_cache(
        kubectl, namespace, execution_name,
        {**configuration, "storageClassName": SOURCE_BUILD_STORAGE_CLASS},
        binding=binding, timeout=timeout, recorded_caches=recorded_caches, owner=owner,
    )
    if execution is None:
        raise DeploymentError(
            f"execution PVC {namespace}/{execution_name} has no proven source binding"
        )
    record_identity(execution)
    return artifact, execution


def application_images(documents: Iterable[dict[str, Any]]) -> set[str]:
    """Read environment images selected by platform defaults, services and workload consumers."""
    images: set[str] = set()
    for document in documents:
        if document.get("metadata", {}).get("labels", {}).get("foretoken.io/platform") == "configuration":
            runtime = _runtime_settings(document)
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
    from foretoken.node_images import node_image_store
    from foretoken.platform.config import default_platform_config
    from foretoken.platform.helm import Helm

    if not uninstall and not _has_server_binding(kubectl):
        return
    directory = _state_directory(kubectl)
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
            references = origin.references(Helm(config).application_history())
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
        context = kubectl.run(["config", "current-context"]).stdout.strip()
        for node in nodes:
            socket = _node_containerd_socket(context, node)
            if socket:
                access.setdefault((node["metadata"]["name"], socket), (tools, secrets))
    for socket in sockets:
        for node in nodes:
            access.setdefault((node["metadata"]["name"], socket), (tools, secrets))
    retained: set[str] = set()
    if not uninstall:
        history = Helm(config).image_history()
        if history is None:
            return
        retained.update(history)
        consumers = application_consumers(kubectl)
        retained.update(application_images(consumers))
        # A service can select a publication before its controller has written the
        # image into status. Preserve that manifest's environment across later builds.
        if any(
            obj.get("kind") in SERVICE_COMPONENTS
            and obj["metadata"].get("annotations", {}).get(SOURCE_REVISION)
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


def _node_containerd_socket(context: str, node: dict[str, Any]) -> str:
    """Resolve the node-local runtime path supported by source import and cleanup."""
    runtime = (
        node.get("status", {}).get("nodeInfo", {}).get("containerRuntimeVersion", "")
    )
    if context.startswith("k3d-") or "k3s" in runtime:
        return "/run/k3s/containerd/containerd.sock"
    if context.startswith("kind-"):
        return "/run/containerd/containerd.sock"
    return ""


def remove_build_caches(kubectl: Kubectl, timeout: str) -> None:
    """Release managed build Pods before their compiler volumes, leaving model caches intact."""
    remove_build_pods(kubectl, timeout)
    kubectl.run(
        [
            "delete",
            "pvc",
            "--all-namespaces",
            "-l",
            _BUILD_CACHE_LABEL + "=true",
            "--ignore-not-found",
            "--timeout=" + timeout,
        ]
    )


def local_build_nodes(
    kubectl: Kubectl, registry: str | None, socket: str = ""
) -> list[tuple[str, str, str]]:
    """Resolve node-local image stores for local and K3s clusters without a registry."""
    if registry:
        return [("", "", "foretoken-compiler-cache")]
    nodes = [
        node
        for node in kubectl.list_resources(("nodes",), "")
        if not node.get("spec", {}).get("unschedulable")
    ]
    if not nodes:
        raise DeploymentError("source builds need a schedulable Kubernetes node")
    if not socket:
        context = kubectl.run(["config", "current-context"]).stdout.strip()
        sockets = {_node_containerd_socket(context, node) for node in nodes}
        if "" in sockets or len(sockets) != 1:
            raise DeploymentError(
                "source installation on a remote cluster requires --registry"
            )
        socket = sockets.pop()
    return [
        (
            node["metadata"]["name"],
            socket,
            "foretoken-compiler-cache-" + node["metadata"]["uid"][:8],
        )
        for node in nodes
    ]


@contextmanager
def prepare_source_images(
    command: InstallCommand,
    namespace: str,
    values: Sequence[dict[str, Any]],
    inference_engine_image: str | None = None,
    *,
    origin: ApplicationFiles,
    build_metax_runtime: bool = False,
    runtime_backend: str = "nvidia",
    saved_arguments: dict[str, str] | None = None,
    before_delivery: Callable[[], None] | None = None,
    checkout: Path | None = None,
    components: tuple[str, ...] = ("control-plane", "frontend", "model-server"),
    cancellation: BuildCancellation | None = None,
) -> Iterator[SourceImages]:
    """Prepare installation artifacts, awaiting dependencies before node image delivery.

    The platform supplies before_delivery when preparing dependencies concurrently.
    Captured inputs remain in pending state until the installation or checkout commits them.
    """
    cancellation = cancellation or BuildCancellation()
    cancellation.check()
    root = Path(command.editable or "").expanduser().resolve()
    if (
        not (root / "Makefile").is_file()
        or not (root / "deploy/charts/foretoken/Chart.yaml").is_file()
    ):
        raise DeploymentError(
            f"--editable must reference a Foretoken source root: {root}"
        )
    kubectl = Kubectl()
    engines = engine_source_roots(command.engine_sources)
    state_directory = checkout if checkout is not None else _state_directory(kubectl)
    state_path = state_directory / "install.json"
    previous = json.loads(state_path.read_text()) if state_path.is_file() else {}
    build = previous.get("build", {}) if previous.get("root") == str(root) else {}
    pending_path = state_directory / "build.json"
    pending = json.loads(pending_path.read_text()) if pending_path.is_file() else {}
    if not build and pending.get("root") == str(root):
        build = pending["build"]
    binding = (
        checkout.name if checkout is not None
        else build.get("binding") or previous.get("build", {}).get("binding") or uuid.uuid4().hex
    )
    # Save compiler identity before any stage can fail; recipes are not cache namespaces.
    cache_scope = None
    if engines:
        pending_build = pending.get("build", {}) if pending.get("root") == str(root) and pending.get("build", {}).get("binding") == binding else {}
        saved_scope = pending_build.get("engine_cache_scope") or build.get("engine_cache_scope")
        cache_scope = engine_cache_scope(binding, engines, saved_scope)
    environment = os.environ.copy()
    if command.oci_registry:
        environment["FORETOKEN_OCI_REGISTRY"] = command.oci_registry
    registry_mirrors = build.get("registry_mirrors", {})
    if saved_arguments is None:
        selected = select_build_sources(
            environment,
            previous_environment=build.get("arguments"),
            previous_registry_mirrors=build.get("registry_mirrors"),
        )
        registry_mirrors = selected.registry_mirrors
        environment.update(selected.environment)
        for selection in selected.messages:
            print(f"Source mirror selected: {selection}", flush=True)
        arguments = build_arguments(environment)
    else:
        environment.update(saved_arguments)
        arguments = dict(saved_arguments)
    if build_metax_runtime:
        prepared = cancellation.run(
            [str(root / "deploy/mooncake/prepare-source")],
            cwd=root,
            env=environment,
        )
        if prepared.returncode:
            raise DeploymentError(
                "could not prepare the MetaX runtime's Mooncake source"
            )
    arguments["VLLM_REVISION"] = pinned_rust_revision(root)
    configuration = build_configuration(
        root,
        values,
        arguments.get(
            "BASE_IMAGE_REGISTRY", registry_mirrors.get("docker.io", [""])[0]
        ),
    )
    registry = (command.registry or "").rstrip("/")
    prefix = registry if registry else "docker.io/library/foretoken-dev"
    suffix = uuid.uuid4().hex
    references = {
        component: f"{prefix}/{component}:{suffix}"
        if registry
        else f"{prefix}-{component}:{suffix}"
        for component in components
    }
    environment_references = dict(references)
    if build_metax_runtime:
        for name in ("metax-sdk", "vllm-metax"):
            environment_references[name] = (
                f"{prefix}/{name}:{suffix}" if registry else f"{prefix}-{name}:{suffix}"
            )
    if engines:
        environment_references["engine-source"] = references["model-server"] + "-engine"
    secret_names = tuple(
        secret["name"]
        for value in values
        for secret in value.get("imagePullSecrets", [])
    )
    nodes = local_build_nodes(
        kubectl, command.registry, build.get("containerd_socket", "")
    )
    if checkout is not None:
        nodes = [(node, socket, f"{claim}-{binding[:12]}") for node, socket, claim in nodes]
    if registry:
        claim = find_build_cache(
            kubectl, namespace, binding, origin.node, "/var/cache/foretoken"
        )
        node_uid = kubectl.get("node", origin.node)["metadata"]["uid"][:8]
        default_claim = "foretoken-application-compiler-" + node_uid
        if checkout is not None:
            default_claim += "-" + binding[:12]
        nodes = [(origin.node, "", claim or default_claim)]
    # Keep a failed first installation's compiler cache addressable for retry and
    # uninstall, without replacing an existing successful installation binding.
    # Null inputs precede the first sync; absent inputs in legacy pending state are unknown.
    state_directory.mkdir(parents=True, exist_ok=True)
    _write_json(
        pending_path,
        {
            "root": str(root),
            "context": _context_identity(kubectl),
            **({"inputs": pending.get("inputs")} if not pending or "inputs" in pending else {}),
            "base_image": inference_engine_image,
            "build": {
                "binding": binding,
                **({"engine_cache_scope": cache_scope} if cache_scope is not None else {}),
                "caches": [
                    *build.get("caches", []),
                    *(cache for cache in pending.get("build", {}).get("caches", [])
                      if cache not in build.get("caches", [])),
                ],
                "images": environment_references,
                "containerd_socket": nodes[0][1],
                "arguments": arguments,
                "registry_mirrors": registry_mirrors,
                "pull_secrets": list(secret_names),
            },
        },
    )
    cancellation.check()
    if checkout is None:
        prepare_build_storage(
            kubectl, namespace, arguments,
            registry_mirrors, secret_names, command.timeout,
        )
    pending_state = json.loads(pending_path.read_text())
    caches = pending_state["build"]["caches"]

    def record_cache(identity: dict[str, Any]) -> None:
        """Persist each created volume before cancellation or another allocation can fail."""
        if identity not in caches:
            caches.append(identity)
            _write_json(pending_path, pending_state)
        cancellation.check()

    execution_claims = {}
    for _, _, claim in nodes:
        cancellation.check()
        _, execution = ensure_build_volumes(
            kubectl, namespace, claim, configuration,
            binding=binding, timeout=command.timeout, record_identity=record_cache,
            recorded_caches=caches,
            owner={
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "name": origin.claim, "uid": origin.claim_uid,
            },
        )
        execution_claims[claim] = execution["name"] if execution is not None else ""
    with capture_build_inputs(root, engines) as snapshot, ExitStack() as build_contexts:
        # Pending inputs survive a failed build: compiler storage is never their only copy.
        with _local_candidates(state_directory):
            destination = state_directory / snapshot.name
            snapshot.rename(destination)
            snapshot = destination
            pending_state["inputs"] = snapshot.name
            _write_json(pending_path, pending_state)
        # Resolve automatic defaults from the same inputs used by the build, without
        # recording them as an explicit base selection in the installation binding.
        base_image = (
            None
            if build_metax_runtime
            else resolve_vllm_base_image(
                snapshot,
                inference_engine_image,
                arguments.get("BASE_IMAGE_REGISTRY", ""),
            )
        )
        credentials = registry_credentials(
            [
                configuration["image"],
                prefix,
                base_image or "",
                "docker.io",
                "gcr.io",
                "ghcr.io",
                *(
                    endpoint
                    for endpoints in registry_mirrors.values()
                    for endpoint in endpoints
                ),
                *(
                    value
                    for key, value in arguments.items()
                    if key.endswith(("REGISTRY", "_IMAGE"))
                ),
            ]
        )
        old = state_directory / previous["inputs"] if previous.get("inputs") else None
        versions = snapshot_versions(snapshot, old, build.get("versions", {}))
        files = {
            str(path.relative_to(snapshot)): path
            for path in snapshot.rglob("*")
            if path.is_file()
        }
        engine_caches: dict[str, str] = {}
        engine_native = bool(engines) and (
            runtime_backend != "metax" or "vllm-metax" in engines
        )
        builders = []
        for node, socket, claim in nodes:
            builder = build_contexts.enter_context(
                ClusterBuilder(
                    kubectl,
                    namespace,
                    claim,
                    "/var/cache/foretoken",
                    configuration["image"],
                    binding,
                    command.timeout,
                    tools_image=image_tools_image(arguments, registry_mirrors),
                    execution_claim=execution_claims[claim],
                    node=node,
                    containerd_socket=socket,
                    pull_secrets=secret_names,
                    credentials=credentials,
                    registry_mirrors=registry_mirrors,
                    cancellation=cancellation,
                )
            )
            builders.append(builder)
            builder.sync(files, versions)

        environments = BuildEnvironments(
            builders, versions,
            build.get("environments", {}) if build.get("registry") == registry else {},
            environment_references, push=bool(registry), refresh=checkout is None,
        )
        publisher = next(builder for builder in builders if builder.node == origin.node)
        changed = {
            path for path in versions.keys() | build.get("versions", {}).keys()
            if path not in _SNAPSHOT_METADATA
            and versions.get(path) != build.get("versions", {}).get(path)
        }
        prior_revisions = {
            component: previous.get("bundles", {})
            .get(component, build.get("applications", {}).get(component, {}))
            .get("revision", "")
            for component in components
        }
        application_parts: dict[str, list[str] | None] = {}

        def build_component(component: str, dockerfile: str) -> dict[str, Any]:
            """Prepare each environment once per node, then export its application at the origin."""
            engine_image = base_image
            if component == "model-server" and build_metax_runtime:
                runtime_arguments = {
                    key: value for key, value in arguments.items()
                    if key in {
                        "METAX_SDK_IMAGE", "UV_IMAGE_REGISTRY", "UV_IMAGE", "MACA_PATH", "UV_PYTHON",
                        "FORETOKEN_GITHUB_MIRROR", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL",
                        "BUILD_JOBS", "BUILDKIT_SYNTAX_IMAGE",
                    }
                }
                if not runtime_arguments.get("METAX_SDK_IMAGE"):
                    sdk = environments.build(
                        "metax-sdk", "deploy/inference-engines/vllm-metax/Dockerfile",
                        target="metax-sdk",
                        arguments={
                            key: value for key, value in arguments.items()
                            if key in {
                                "BASE_IMAGE_REGISTRY", "UV_IMAGE_REGISTRY", "UV_IMAGE",
                                "BUILDKIT_SYNTAX_IMAGE", "BUILD_JOBS", "FORETOKEN_GITHUB_MIRROR",
                                "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL",
                            }
                        },
                        input_prefixes=(
                            "deploy/inference-engines/vllm-metax/prepare-sdk.sh",
                            "deploy/inference-engines/vllm-metax/torch_cpu_config.py",
                            "deploy/inference-engines/vllm-metax/sdk-environment.json",
                        ),
                    )
                    runtime_arguments["METAX_SDK_IMAGE"] = sdk["image"]
                runtime = environments.build(
                    "vllm-metax", "deploy/inference-engines/vllm-metax/Dockerfile",
                    arguments=runtime_arguments,
                    input_prefixes=(
                        "deploy/inference-engines/vllm-metax/install.sh",
                        "deploy/inference-engines/vllm-metax/sdk_audio.py",
                        "deploy/inference-engines/vllm-metax/source-environment.json",
                        "deploy/inference-engines/vllm-metax/patches/",
                        "deploy/mooncake/", "third_party/mooncake/",
                    ),
                )
                engine_image = runtime["image"]
            if component == "model-server":
                dependency_names = {
                    "BASE_IMAGE_REGISTRY", "UV_IMAGE_REGISTRY", "UV_IMAGE", "UV_DEFAULT_INDEX",
                    "FORETOKEN_GITHUB_MIRROR", "FORETOKEN_VLLM_PYTHON", "BUILDKIT_SYNTAX_IMAGE",
                }
            else:
                dependency_names = {
                    "BUILDKIT_SYNTAX_IMAGE",
                    "DISTROLESS_IMAGE_REGISTRY" if component == "control-plane" else "BASE_IMAGE_REGISTRY",
                }
            dependency_arguments = {
                key: value for key, value in arguments.items() if key in dependency_names
            }
            if component == "model-server" and engine_image:
                dependency_arguments["INFERENCE_ENGINE_IMAGE"] = engine_image
            selected = environments.build(
                component, dockerfile, target="environment",
                arguments=dependency_arguments,
                input_prefixes=("data-plane/patches/vllm/",) if component == "model-server" else (),
            )
            prior = prior_revisions[component]
            manifest = origin.read_manifest(origin.reference(component, prior)) if prior else None
            parts = None
            # Reuse only files from a completed application whose dependency base
            # and compiler inputs still match. Engine binding does not change Rust.
            if (
                component != "control-plane" and manifest is not None
                and manifest.get("image") == previous.get("runtime", {}).get(
                    "model_image" if component == "model-server" else "image"
                )
                and selected["image"] == build.get("images", {}).get("environment-" + component)
                and arguments == build.get("arguments")
            ):
                selected_parts = {"rust"} if component == "frontend" else {"rust", "python", "engine"}
                if not any(
                    path in {".dockerignore", "LICENSE"}
                    or (path.startswith("data-plane/") and not path.startswith("data-plane/model-server/python/"))
                    for path in changed
                ):
                    selected_parts.discard("rust")
                if not any(path.startswith("data-plane/model-server/python/") for path in changed):
                    selected_parts.discard("python")
                parts = sorted(selected_parts)
            application_parts[component] = parts
            # Join the file export before publication or releasing its workspace.
            with ThreadPoolExecutor(max_workers=1) as application_build, cancellation:
                payload = publisher.root + "/applications/" + component
                application = application_build.submit(
                    cancellation.run_stage, publisher.export_application, component, payload, arguments, parts,
                )
                if component == "model-server" and engines:
                    cache_prefix = cache_scope["prefix"]
                    engine_arguments = {
                        key: value for key, value in engine_build_arguments(arguments).items()
                        if key not in {"BUILD_JOBS", "TARGET_CUDA_ARCH_LIST"}
                    }
                    engine_arguments["RUNTIME_IMAGE"] = selected["image"]
                    node_arguments = {}
                    # Dependency exports can acquire new OCI digests on every retry.
                    # The engine base and selected interpreter own Python/accelerator ABI;
                    # CMake and ccache track native source/tool inputs within that environment.
                    python_executable = arguments.get("FORETOKEN_VLLM_PYTHON") or "python3"
                    for builder in builders:
                        base_digest = builder.resolve_image(engine_image)
                        node_arguments[builder.node] = {
                            "RUNTIME_USER": builder.image_user(selected["image"]),
                            "CACHE_ID": f"{cache_prefix}-metadata-{base_digest}-{python_executable}",
                        }
                        key = "" if registry else builder.node
                        engine_caches[key] = f"{cache_prefix}-native-{base_digest}-{python_executable}"
                    pending_state["build"]["engine_caches"] = dict(engine_caches)
                    _write_json(pending_path, pending_state)
                    selected = environments.build(
                        "engine-source", "deploy/inference-engines/source-build.Dockerfile",
                        target="environment", arguments=engine_arguments,
                        node_arguments=node_arguments,
                        input_prefixes=(
                            "engine/", "deploy/inference-engines/source-build.py",
                            "deploy/inference-engines/vllm-metax/source-environment.json",
                            "deploy/inference-engines/vllm-metax/sdk_audio.py",
                            "deploy/inference-engines/vllm-metax/patches/",
                            "data-plane/patches/vllm/",
                        ),
                    )
                application.result()
                if component == "model-server" and engines:
                    engine_output = publisher.root + "/applications/engine"
                    publisher.build(
                        "deploy/inference-engines/source-build.Dockerfile",
                        target="source-export", destination=engine_output,
                        arguments={
                            **engine_build_arguments(arguments),
                            "RUNTIME_IMAGE": selected["image"],
                            "CACHE_ID": engine_caches["" if registry else origin.node],
                            "BUILD_NATIVE": str(engine_native).lower(),
                        },
                    )
                    publisher.run([
                        "sh", "-ec", 'cp -R "$1/." "$2/"', "assemble", engine_output, payload,
                    ], container=publisher.artifact_container)
                return selected

        with ThreadPoolExecutor(max_workers=len(references)) as builds, cancellation:
            pending_builds = {
                component: builds.submit(cancellation.run_stage, build_component, component, dockerfile)
                for component, dockerfile in (
                    ("control-plane", "control-plane/Dockerfile"),
                    ("frontend", "data-plane/frontend/Dockerfile"),
                    ("model-server", "data-plane/model-server/Dockerfile"),
                ) if component in components
            }
            selected_environments = {
                component: future.result() for component, future in pending_builds.items()
            }
        digests: dict[str, dict[str, str]] = {builder.node: {} for builder in builders}
        for component, record in selected_environments.items():
            references[component] = record["image"]
            for node, digest in record["digests"].items():
                digests[node][component] = digest
        # The publisher compares actual exports, independently of download-source choices.
        # Prefer the latest deployment bundle over the initial installation's application.
        retained_applications = None
        if checkout is not None:
            from foretoken.platform.config import default_platform_config
            from foretoken.platform.helm import Helm

            retained_applications = origin.references(
                Helm(default_platform_config(command.oci_registry)).application_history()
            )
            if retained_applications is not None:
                retained_applications.add(suffix)
                retained_applications.update(
                    bundle["revision"]
                    for bundle in [
                        *previous.get("bundles", {}).values(),
                        *build.get("applications", {}).values(),
                    ]
                )
        # Candidate GC is binding-scoped; retain every in-flight component until
        # service intent is committed. Platform publication defers GC to install.
        with ThreadPoolExecutor(max_workers=len(references)) as publications, cancellation:
            pending = {}
            for component, image in references.items():
                prior = prior_revisions[component]
                pending[component] = publications.submit(
                    cancellation.run_stage, origin.publish,
                    publisher,
                    publisher.root + "/applications/" + component,
                    component,
                    suffix,
                    prior,
                    retained_applications,
                    timeout=command.timeout,
                    reuse_previous=True,
                    sources=build_sources(snapshot, component),
                    image=image,
                    parts=application_parts[component],
                    previous_image=previous.get("runtime", {}).get(
                        "model_image" if component == "model-server" else "image"
                    ),
                )
            application_revisions = {
                component: future.result() for component, future in pending.items()
            }
        publisher.run(["rm", "-rf", "--", publisher.root + "/applications"], container=publisher.artifact_container)
        applications = {
            component: origin.reference(component, revision)
            for component, revision in application_revisions.items()
        }
        # Keep builders alive until platform dependencies finish. A failed
        # prerequisite exits through cleanup without delivering candidate images.
        if before_delivery is not None:
            before_delivery()
        for name, record in environments.records.items():
            for node, digest in record["digests"].items():
                digests[node]["environment-" + name] = digest
        environment_images = {
            "environment-" + name: record["image"]
            for name, record in environments.records.items()
        }
        for builder in builders:
            environments.deliver(builder)
            builder.discard_unselected_images(
                set(references.values()) | set(environment_images.values())
            )
        build_contexts.close()
        validate_build_inputs(root, snapshot, engines)
        yield SourceImages(
            root,
            "registry" if registry else "import",
            references.get("control-plane", ""),
            references["frontend"],
            references["model-server"],
            snapshot,
            {
                "binding": binding,
                "caches": caches,
                "configuration": configuration,
                "versions": versions,
                "digests": digests,
                "images": {**references, **environment_images},
                "environments": environments.records,
                "arguments": arguments,
                "registry_mirrors": registry_mirrors,
                "registry": registry,
                "containerd_socket": nodes[0][1],
                "engine_caches": engine_caches,
                **({"engine_cache_scope": cache_scope} if cache_scope is not None else {}),
                "engine_native": engine_native,
                "runtime_backend": runtime_backend,
                "build_metax_runtime": build_metax_runtime,
                "applications": {
                    component: {"revision": revision}
                    for component, revision in application_revisions.items()
                },
            },
            applications,
        )

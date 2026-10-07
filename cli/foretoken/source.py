# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own source installation bindings, build inputs and platform application preparation."""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from foretoken.application_files import ApplicationFiles, remove_application_jobs
from foretoken.arguments import InstallCommand
from foretoken.cluster_build import (
    ClusterBuilder,
    find_build_cache,
    registry_credentials,
    remove_build_pods,
)
from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError
from foretoken.network_sources import select_source_build_sources

_BUILD_CACHE_LABEL = "inference.foretoken.io/source-build-cache"
_INSTALL_SOURCE = "foretoken.io/install-source"


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
    kubectl: Kubectl, timeout: str, *, installing: bool = False
) -> Iterator[None]:
    """Serialize CLI source operations by cluster, including artifact readers and cleanup.

    The CLI entry point holds this lock through install, deploy, or uninstall. Internal
    image rebuilds call the platform lifecycle directly within the same operation.
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
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
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
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


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
) -> None:
    """Save exact input bytes, reusing already-compared snapshot files without copying them."""
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
    """Own candidate image inputs until record_install moves them into committed state."""
    destination = _source_home() / "builds" / ("inputs-" + uuid.uuid4().hex)
    try:
        _snapshot(_inputs(root, engines), destination, engines)
        yield destination
    finally:
        if destination.exists():
            shutil.rmtree(destination)


@contextmanager
def _local_candidates(directory: Path) -> Iterator[None]:
    """Retire unreferenced local artifacts after a state commit or failed preparation.

    The cluster operation lock keeps other CLI readers out during retirement. Running
    workloads consume their published cache copies rather than these local candidates.
    """
    try:
        yield
    finally:
        path = directory / "install.json"
        state = json.loads(path.read_text()) if path.is_file() else {}
        for snapshot in directory.glob("inputs-*"):
            if snapshot.name != state.get("inputs"):
                shutil.rmtree(snapshot)
        if (directory / "bundles").is_dir():
            shutil.rmtree(directory / "bundles")


def has_source_state(kubectl: Kubectl) -> bool:
    """Identify installed or interrupted source builds belonging to this actual cluster."""
    if not _has_server_binding(kubectl):
        return False
    directory = _state_directory(kubectl)
    return any((directory / name).is_file() for name in ("install.json", "build.json"))


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
        and str(path.relative_to(snapshot))
        not in {"engine/manifest.json", "engine/deleted.json"}
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
            name not in changed
            if changed is not None
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
    """Read the controller-owned runtime environment used to bind and validate source builds."""
    arguments = platform["spec"]["template"]["spec"]["containers"][0]["args"]
    options = dict(
        arg[2:].split("=", 1)
        for arg in arguments
        if arg.startswith("--") and "=" in arg
    )
    return {
        "image": options["frontend-image"],
        "model_image": options["inference-engine-image"],
        "omni_image": options.get("omni-inference-engine-image", ""),
        "nsight_image": options.get("nsight-image", ""),
        "mount": options["cache-mount-path"],
        "claim": options.get("cache-claim", ""),
        "pull_secrets": [
            arg.split("=", 1)[1]
            for arg in arguments
            if arg.startswith("--workload-image-pull-secret=")
        ],
    }


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
    platforms = kubectl.list_all_resources(
        ("deployment.apps",),
        label_selector="app.kubernetes.io/name=foretoken-control-plane",
    )
    managed = [
        p
        for p in platforms
        if p["metadata"].get("annotations", {}).get(_INSTALL_SOURCE) == "source"
    ]
    if len(managed) != 1:
        raise DeploymentError("expected one source-installed Foretoken platform")
    with _local_candidates(directory):
        destination = directory / snapshot.name
        snapshot.rename(destination)
        _write_json(
            directory / "install.json",
            {
                "root": str(root),
                "engines": engines,
                "build": build_state or {},
                "context": _context_identity(kubectl),
                "inputs": destination.name,
                "platform_uid": managed[0]["metadata"]["uid"],
                "base_image": base_image,
                "runtime": _runtime_settings(managed[0]),
                "command": settings,
                "bundles": {},
            },
        )
        (directory / "build.json").unlink(missing_ok=True)


@dataclass(frozen=True)
class SourceImages:
    """Environment images, application references and source inputs committed after installation."""

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


def image_tools_image(arguments: dict[str, str]) -> str:
    """Select Bash and GNU tar for node image operations using the build's Docker mirror."""
    registry = arguments.get("BASE_IMAGE_REGISTRY", "docker.io").rstrip("/")
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
        result.update(UV_IMAGE_REGISTRY=ghcr, INFERENCE_ENGINE_IMAGE_REGISTRY=ghcr)
    if gcr:
        result["DISTROLESS_IMAGE_REGISTRY"] = gcr
    return result


def ensure_build_cache(
    kubectl: Kubectl,
    namespace: str,
    name: str,
    configuration: dict[str, str],
    *,
    owner: dict[str, str] | None = None,
) -> None:
    """Create or resize the compiler cache without taking over an unrelated volume."""
    if not kubectl.exists("namespace", namespace):
        kubectl.run(["create", "namespace", namespace])
    existing = kubectl.get_if_exists("pvc", name, namespace)
    if existing is not None:
        if existing["metadata"].get("labels", {}).get(_BUILD_CACHE_LABEL) != "true":
            raise DeploymentError(f"{namespace}/{name} is not a Foretoken build cache")
        spec = {"resources": {"requests": {"storage": configuration["storageSize"]}}}
        if configuration["storageClassName"]:
            spec["storageClassName"] = configuration["storageClassName"]
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
        return
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": configuration["storageSize"]}},
    }
    if configuration["storageClassName"]:
        spec["storageClassName"] = configuration["storageClassName"]
    kubectl.run(
        ["create", "-f", "-"],
        input_text=yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {_BUILD_CACHE_LABEL: "true"},
                    **({"ownerReferences": [owner]} if owner is not None else {}),
                },
                "spec": spec,
            }
        ),
    )


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
        return [("", "", "foretoken-source-build")]
    nodes = [
        node
        for node in kubectl.list_resources(("nodes",), "")
        if not node.get("spec", {}).get("unschedulable")
    ]
    if not nodes:
        raise DeploymentError("source builds need a schedulable Kubernetes node")
    if not socket:
        context = kubectl.run(["config", "current-context"]).stdout.strip()
        if context.startswith("k3d-"):
            socket = "/run/k3s/containerd/containerd.sock"
        elif context.startswith("kind-"):
            socket = "/run/containerd/containerd.sock"
        elif all(
            "k3s"
            in node.get("status", {})
            .get("nodeInfo", {})
            .get("containerRuntimeVersion", "")
            for node in nodes
        ):
            socket = "/run/k3s/containerd/containerd.sock"
        else:
            raise DeploymentError(
                "source installation on a remote cluster requires --registry"
            )
    return [
        (
            node["metadata"]["name"],
            socket,
            "foretoken-source-build-" + node["metadata"]["uid"][:8],
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
    installed_images: tuple[str, str, str] | None = None,
    build_metax_runtime: bool = False,
    runtime_backend: str = "nvidia",
    saved_arguments: dict[str, str] | None = None,
) -> Iterator[SourceImages]:
    """Build directly in the target cluster and retain inputs until installation commits."""
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
    state_directory = _state_directory(kubectl)
    state_path = state_directory / "install.json"
    previous = json.loads(state_path.read_text()) if state_path.is_file() else {}
    build = previous.get("build", {}) if previous.get("root") == str(root) else {}
    pending_path = state_directory / "build.json"
    pending = json.loads(pending_path.read_text()) if pending_path.is_file() else {}
    if not build and pending.get("root") == str(root):
        build = pending["build"]
    binding = build.get("binding") or uuid.uuid4().hex
    environment = os.environ.copy()
    if command.oci_registry:
        environment["FORETOKEN_OCI_REGISTRY"] = command.oci_registry
    if saved_arguments is None:
        selected, selections, _ = select_source_build_sources(environment)
        environment.update(selected)
        for selection in selections:
            print(f"Source mirror selected: {selection}", flush=True)
        arguments = build_arguments(environment)
    else:
        environment.update(saved_arguments)
        arguments = dict(saved_arguments)
    if build_metax_runtime:
        prepared = subprocess.run(
            [str(root / "deploy/mooncake/prepare-source")],
            cwd=root,
            env=environment,
            check=False,
        )
        if prepared.returncode:
            raise DeploymentError(
                "could not prepare the MetaX runtime's Mooncake source"
            )
    arguments["VLLM_REVISION"] = pinned_rust_revision(root)
    configuration = build_configuration(
        root, values, arguments.get("BASE_IMAGE_REGISTRY", "")
    )
    registry = (command.registry or "").rstrip("/")
    prefix = registry if registry else "docker.io/library/foretoken-dev"
    suffix = uuid.uuid4().hex
    references = {
        component: f"{prefix}/{component}:{suffix}"
        if registry
        else f"{prefix}-{component}:{suffix}"
        for component in ("control-plane", "frontend", "model-server")
    }
    secret_names = tuple(
        secret["name"]
        for value in values
        for secret in value.get("imagePullSecrets", [])
    )
    credentials = registry_credentials(
        [
            configuration["image"],
            prefix,
            inference_engine_image or "",
            arguments.get("UV_IMAGE", ""),
            "docker.io",
            "gcr.io",
            "ghcr.io",
            *(value for key, value in arguments.items() if key.endswith("REGISTRY")),
        ]
    )
    remove_application_jobs(kubectl, binding, command.timeout)
    origin = ApplicationFiles(kubectl, namespace)
    origin.prepare(command.timeout)
    nodes = local_build_nodes(
        kubectl, command.registry, build.get("containerd_socket", "")
    )
    if registry:
        claim = find_build_cache(
            kubectl, namespace, binding, origin.node, "/var/cache/foretoken"
        )
        node_uid = kubectl.get("node", origin.node)["metadata"]["uid"][:8]
        nodes = [(origin.node, "", claim or "foretoken-application-build-" + node_uid)]
    applications = {
        component: origin.reference(component, suffix) for component in references
    }
    # Keep a failed first installation's compiler cache addressable for retry and
    # uninstall, without replacing an existing successful installation binding.
    state_directory.mkdir(parents=True, exist_ok=True)
    _write_json(
        pending_path,
        {
            "root": str(root),
            "context": _context_identity(kubectl),
            "build": {"binding": binding, "containerd_socket": nodes[0][1]},
        },
    )
    for _, _, claim in nodes:
        ensure_build_cache(kubectl, namespace, claim, configuration)
    with capture_build_inputs(root, engines) as snapshot, ExitStack() as build_contexts:
        old = state_directory / previous["inputs"] if previous.get("inputs") else None
        versions = snapshot_versions(snapshot, old, build.get("versions", {}))
        files = {
            str(path.relative_to(snapshot)): path
            for path in snapshot.rglob("*")
            if path.is_file()
        }
        digests: dict[str, dict[str, str]] = {}
        engine_caches: dict[str, str] = {}
        engine_native = bool(engines) and (
            runtime_backend != "metax" or "vllm-metax" in engines
        )
        installed = dict(zip(references, installed_images or (), strict=False))
        reusable = {
            component: build.get("registry") == registry
            and installed.get(component) == build.get("images", {}).get(component)
            and component in installed
            for component in references
        }
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
                    tools_image=image_tools_image(arguments),
                    node=node,
                    containerd_socket=socket,
                    pull_secrets=secret_names,
                    credentials=credentials,
                )
            )
            builders.append(builder)
            builder.sync(files, versions)
            engine_image = inference_engine_image
            if build_metax_runtime:
                engine_image = (
                    f"{prefix}/vllm-metax:{suffix}"
                    if registry
                    else f"{prefix}-vllm-metax:{suffix}"
                )
                builder.build(
                    "deploy/inference-engines/vllm-metax/Dockerfile",
                    image=engine_image,
                    push=bool(registry),
                    arguments=arguments,
                )
            node_digests = {}
            for component, dockerfile in (
                ("control-plane", "control-plane/Dockerfile"),
                ("frontend", "data-plane/frontend/Dockerfile"),
                ("model-server", "data-plane/model-server/Dockerfile"),
            ):
                component_arguments = dict(arguments)
                if component == "model-server" and engine_image:
                    component_arguments["INFERENCE_ENGINE_IMAGE"] = engine_image
                print(
                    f"Building {component} in {namespace}/{builder.name}",
                    flush=True,
                )
                image = (
                    references[component] + "-base"
                    if component == "model-server" and engines
                    else references[component]
                )
                metadata = builder.build(
                    dockerfile,
                    target="environment",
                    image=image,
                    push=bool(registry),
                    arguments=component_arguments,
                    reuse_image=installed.get(component, "")
                    if image == references[component]
                    else "",
                )
                final_dockerfile, final_target, final_arguments = (
                    dockerfile,
                    "environment",
                    component_arguments,
                )
                if component == "model-server" and engines:
                    cache_prefix = "-".join(
                        (
                            binding,
                            versions[
                                "deploy/inference-engines/source-build.Dockerfile"
                            ],
                            *sorted(engines),
                        )
                    )
                    engine_arguments = {
                        **arguments,
                        "RUNTIME_IMAGE": image,
                        "CACHE_ID": cache_prefix
                        + "-"
                        + metadata["containerimage.digest"],
                    }
                    user_output = builder.root + "/runtime-user"
                    builder.build(
                        "deploy/inference-engines/source-build.Dockerfile",
                        target="runtime-user-export",
                        destination=user_output,
                        arguments=engine_arguments,
                    )
                    engine_arguments["RUNTIME_USER"] = builder.run(
                        ["cat", user_output + "/user"], capture=True
                    ).strip()
                    metadata = builder.build(
                        "deploy/inference-engines/source-build.Dockerfile",
                        target="environment",
                        image=references[component],
                        push=bool(registry),
                        arguments=engine_arguments,
                        reuse_image=installed.get(component, ""),
                    )
                    final_dockerfile, final_target, final_arguments = (
                        "deploy/inference-engines/source-build.Dockerfile",
                        "environment",
                        engine_arguments,
                    )
                    # Native exports use the selected dependency environment, and their
                    # cache identity survives Python-only updates and origin relocation.
                    engine_cache = (
                        cache_prefix + "-" + metadata["containerimage.digest"]
                    )
                    engine_caches["" if registry else node] = engine_cache
                    engine_export_arguments = {
                        **engine_arguments,
                        "RUNTIME_IMAGE": references[component],
                        "CACHE_ID": engine_cache,
                        "BUILD_NATIVE": str(engine_native).lower(),
                    }
                node_digests[component] = metadata["containerimage.digest"]
                reusable[component] = reusable[component] and node_digests[
                    component
                ] == build.get("digests", {}).get(node, {}).get(component)
                if reusable[component]:
                    # Restore an unchanged reference even if its registry tag or local
                    # image record was collected; never overwrite it with new content.
                    if socket:
                        builder.reuse_image_reference(
                            references[component], installed[component]
                        )
                    else:
                        builder.build(
                            final_dockerfile,
                            target=final_target,
                            image=installed[component],
                            push=True,
                            arguments=final_arguments,
                        )
                if node == origin.node:
                    payload = builder.root + "/applications/" + component
                    builder.build(
                        dockerfile,
                        target="source-export",
                        destination=payload,
                        arguments=component_arguments,
                    )
                    if component == "model-server" and engines:
                        engine_output = builder.root + "/applications/engine"
                        builder.build(
                            "deploy/inference-engines/source-build.Dockerfile",
                            target="source-export",
                            destination=engine_output,
                            arguments=engine_export_arguments,
                        )
                        builder.run(
                            [
                                "sh",
                                "-ec",
                                'cp -R "$1/." "$2/"',
                                "assemble",
                                engine_output,
                                payload,
                            ]
                        )
                    origin.publish(
                        builder,
                        payload,
                        component,
                        suffix,
                        "",
                        None,
                        timeout=command.timeout,
                    )
            if node == origin.node:
                builder.run(["rm", "-rf", "--", builder.root + "/applications"])
            digests[node] = node_digests
        for component in references:
            if reusable[component]:
                references[component] = installed[component]
        for builder in builders:
            builder.discard_unselected_images(
                set(references.values()) | set(installed.values())
            )
        build_contexts.close()
        validate_build_inputs(root, snapshot, engines)
        yield SourceImages(
            root,
            "registry" if registry else "import",
            references["control-plane"],
            references["frontend"],
            references["model-server"],
            snapshot,
            {
                "binding": binding,
                "configuration": configuration,
                "versions": versions,
                "digests": digests,
                "images": references,
                "arguments": arguments,
                "registry": registry,
                "containerd_socket": nodes[0][1],
                "engine_caches": engine_caches,
                "engine_native": engine_native,
                "applications": {
                    component: {"revision": suffix} for component in references
                },
            },
            applications,
        )

"""Create and remove local kind and k3d development clusters."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from foretoken.arguments import ClusterCommand
from foretoken.manifest import DeploymentError

_DEVICE_PLUGIN = (
    "https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/"
    "v0.17.4/deployments/static/nvidia-device-plugin.yml"
)


def _run(arguments: Sequence[str]) -> None:
    """Run one host cluster command and surface its failure to the CLI caller."""
    try:
        subprocess.run(arguments, check=True)
    except FileNotFoundError as exc:
        raise DeploymentError(f"missing required command: {arguments[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise DeploymentError(
            f"cluster command failed with exit code {exc.returncode}: {' '.join(arguments)}"
        ) from exc


def _require_commands(commands: Sequence[str]) -> None:
    """Check host tools and Docker access before creating a local cluster."""
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise DeploymentError("missing required command(s): " + ", ".join(missing))
    if "docker" in commands:
        access = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, check=False
        )
        if access.returncode:
            raise DeploymentError(
                "the current user cannot access Docker; configure Docker permissions "
                "and verify with `docker info` without sudo"
            )


def _mount_arguments() -> list[str]:
    """Collect NVIDIA runtime files that must be visible inside a GPU k3d node."""
    arguments: list[str] = []
    mounted: set[Path] = set()

    def add(path: str | Path) -> None:
        value = Path(path)
        if not value.exists() or value in mounted:
            return
        mounted.add(value)
        arguments.extend(("--volume", f"{value}:{value}@server:0"))

    for name in (
        "nvidia-container-runtime",
        "nvidia-container-runtime-hook",
        "nvidia-container-cli",
        "nvidia-ctk",
    ):
        tool = shutil.which(name)
        if tool is None:
            continue
        add(tool)
        result = subprocess.run(
            ["ldd", tool], capture_output=True, text=True, check=False
        )
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[1] == "=>" and fields[2].startswith("/"):
                add(Path(fields[2]).parent)
            elif fields and fields[0].startswith("/"):
                add(fields[0])

    for path in ("/etc/nvidia-container-runtime", "/usr/local/etc/nvidia-container-runtime"):
        add(path)
    for path in (shutil.which("ldconfig"), "/sbin/ldconfig.real", "/usr/sbin/ldconfig.real"):
        if path:
            add(path)
    return arguments


def _refresh_kubeconfig(kind: str, name: str) -> None:
    """Refresh and select the kubeconfig entry written by a local cluster tool."""
    if kind == "kind":
        _run(["kind", "export", "kubeconfig", "--name", name])
    else:
        _run(
            [
                "k3d",
                "kubeconfig",
                "merge",
                name,
                "--kubeconfig-merge-default",
                "--kubeconfig-switch-context",
            ]
        )


def _create_kind(command: ClusterCommand, root: Path) -> None:
    """Create a local CPU-oriented kind cluster and select its kubeconfig context."""
    _require_commands(("kind", "kubectl"))
    config = Path(command.config) if command.config else root / "deploy/kind/multi-node.yaml"
    if not config.is_file():
        raise DeploymentError(f"kind configuration not found: {config}")
    _run(["kind", "create", "cluster", "--name", command.name, "--config", str(config), "--wait", "5m"])
    _refresh_kubeconfig("kind", command.name)
    _run(["kubectl", "cluster-info", "--context", f"kind-{command.name}"])


def _create_k3d(command: ClusterCommand, root: Path) -> None:
    """Create a GPU-restricted k3d cluster with its NVIDIA device plugin."""
    _require_commands(("docker", "k3d", "kubectl"))
    config = Path(command.config) if command.config else root / "deploy/k3d/config.yaml"
    if not config.is_file():
        raise DeploymentError(f"k3d configuration not found: {config}")
    data = root / "data"
    data.mkdir(exist_ok=True)
    arguments = [
        "k3d", "cluster", "create", command.name,
        "--config", str(config),
        "--gpus", f'"device={command.gpus}"',
        "--volume", f"{data}:{data}@server:0",
        *_mount_arguments(),
    ]
    _run(arguments)
    _refresh_kubeconfig("k3d", command.name)
    _run(["kubectl", "apply", "-f", _DEVICE_PLUGIN])
    _run([
        "kubectl", "set", "env", "daemonset/nvidia-device-plugin-daemonset",
        "--namespace", "kube-system", f"NVIDIA_VISIBLE_DEVICES={command.gpus}",
    ])
    _run([
        "kubectl", "rollout", "status", "daemonset/nvidia-device-plugin-daemonset",
        "--namespace", "kube-system", "--timeout=5m",
    ])


def _delete(command: ClusterCommand) -> None:
    """Delete one explicitly named local kind or k3d cluster."""
    tool = "kind" if command.kind == "kind" else "k3d"
    _require_commands((tool,))
    arguments = (
        [tool, "delete", "cluster", command.name]
        if command.kind == "kind"
        else [tool, "cluster", "delete", command.name]
    )
    _run(arguments)


def run(command: ClusterCommand, root: Path | None = None) -> None:
    """Create or remove a local cluster selected by the parsed CLI command."""
    root = root or Path.cwd()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", command.name):
        raise DeploymentError(f"invalid cluster name: {command.name}")
    if command.action == "delete":
        _delete(command)
    elif command.kind == "kind":
        _create_kind(command, root)
    else:
        _create_k3d(command, root)

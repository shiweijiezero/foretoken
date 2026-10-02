# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Apply the selected Foretoken patch series to an installed vLLM package."""

import py_compile
import shutil
import subprocess
import tempfile
from importlib.metadata import distribution
from pathlib import Path

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version


def select_patches(version: Version, site: Path) -> list[Path]:
    """Resolve the installed engine's patch series and optional package features."""
    patch_root = Path(__file__).resolve().parent
    version_map = yaml.safe_load((patch_root / "version-map.yaml").read_text())
    series_name = version_map["default_series"]
    for profile in version_map["profiles"]:
        if SpecifierSet(profile["versions"]).contains(version, prereleases=True):
            series_name = profile["series"]
            break

    patches = []
    for line in (patch_root / series_name).read_text().splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        optional = version_map["optional_patches"].get(name)
        if optional is not None and (
            not SpecifierSet(optional["versions"]).contains(version, prereleases=True)
            or not (site / optional["requires"]).is_file()
        ):
            continue
        patches.append(patch_root / name)
    return patches


def _series_applied(patches: list[Path], site: Path, changed_files: set[Path]) -> bool:
    """Recognize a complete stack even when later patches change earlier context."""
    if not all((site / path).is_file() for path in changed_files):
        return False
    with tempfile.TemporaryDirectory(prefix="vllm-patch-check-") as directory:
        probe = Path(directory)
        for path in changed_files:
            target = probe / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(site / path, target)
        for patch in reversed(patches):
            result = subprocess.run(
                [
                    "patch",
                    "--force",
                    "--reverse",
                    "--fuzz=0",
                    "--strip=1",
                    f"--directory={probe}",
                    f"--input={patch}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode:
                return False
    return True


def main() -> None:
    """Apply patches idempotently and compile each changed Python file once."""
    engine = distribution("vllm")
    site = Path(engine.locate_file(""))
    patches = select_patches(Version(engine.version), site)
    changed_files = {
        Path(line[6:].split()[0])
        for patch in patches
        for line in patch.read_text().splitlines()
        if line.startswith("+++ b/")
    }
    if _series_applied(patches, site, changed_files):
        print(f"vLLM {engine.version}: patch series already installed", flush=True)
    else:
        for patch_file in patches:
            patch_args = [
                "patch",
                "--fuzz=0",
                "--strip=1",
                f"--directory={site}",
                f"--input={patch_file}",
            ]
            reverse = subprocess.run(
                [*patch_args, "--force", "--reverse", "--dry-run"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if reverse.returncode == 0:
                print(f"vLLM patch already installed: {patch_file.name}", flush=True)
            else:
                subprocess.run(
                    [*patch_args, "--batch", "--forward", "--no-backup-if-mismatch"],
                    check=True,
                )

    for path in sorted(changed_files):
        if path.suffix == ".py":
            py_compile.compile(str(site / path), doraise=True)


if __name__ == "__main__":
    main()

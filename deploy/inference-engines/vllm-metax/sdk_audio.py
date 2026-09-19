# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Package the SDK's ABI-matched audio distribution for an isolated 0.26 runtime."""

import json
from email.parser import Parser
from pathlib import Path
import subprocess
import sys

from wheel.wheelfile import WheelFile


def prepare_sdk_audio(prefix: Path) -> None:
    """Retain the SDK package version and pin the plugin to the distribution installed below it.

    The SDK interpreter supplies only torchaudio. Repacking its recorded files
    into a wheel lets the normal resolver install and track it in the new venv;
    the runtime never adds the SDK's site-packages to its search path.
    """
    source = subprocess.check_output(
        [
            sys._base_executable,
            "-c",
            "import importlib.metadata as m, json; import torchaudio; "
            "d=m.distribution('torchaudio'); "
            "print(json.dumps({'version':d.version,'root':str(d.locate_file('')),"
            "'wheel':d.read_text('WHEEL'),'files':[str(f) for f in d.files]}))",
        ],
        text=True,
    )
    distribution = json.loads(source)
    version = distribution["version"]
    metadata = Parser().parsestr(distribution["wheel"])
    tag, = metadata.get_all("Tag")
    package_root = Path(distribution["root"])
    wheel_dir = prefix / "sdk-wheels"
    wheel_dir.mkdir()
    wheel_path = wheel_dir / f"torchaudio-{version}-{tag}.whl"
    with WheelFile(wheel_path, "w") as wheel:
        for entry in distribution["files"]:
            relative = Path(entry)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"SDK torchaudio has a file outside its package root: {entry}")
            if "__pycache__" in relative.parts or (
                relative.parent.name.endswith(".dist-info")
                and relative.name in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}
            ):
                continue
            wheel.write(package_root / relative, entry)
    requirements = prefix / "third_party/vllm-metax/requirements/maca_private.txt"
    content = requirements.read_text()
    original = "torchaudio==2.4.1+metax3.8.2.2"
    if content.count(original) != 1:
        raise ValueError("MetaX 0.26 torchaudio requirement differs from the supported source")
    requirements.write_text(content.replace(original, f"torchaudio=={version}"))
    print(f"Using SDK torchaudio {version} from {sys._base_executable}")


if __name__ == "__main__":
    prepare_sdk_audio(Path(sys.argv[1]))

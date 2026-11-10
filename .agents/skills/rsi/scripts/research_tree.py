# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Render the user-facing Proposal tree by injecting tree data into the HTML template."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

PLACEHOLDER = "__FORETOKEN_TREE_DATA__"
TEMPLATE = Path(__file__).resolve().parents[4] / "rsi-playbooks" / "templates" / "research-tree-template.html"


def render(data: dict, template: Path = TEMPLATE) -> str:
    """Return the template with ``data`` embedded; the page derives labels, states and layout itself."""
    text = template.read_text(encoding="utf-8")
    if text.count(PLACEHOLDER) != 1:
        raise ValueError(f"{template} must contain {PLACEHOLDER} exactly once")
    # "</" would end the surrounding <script> element early.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return text.replace(PLACEHOLDER, payload)


def write(data: dict, output: Path, template: Path = TEMPLATE) -> None:
    """Atomically replace ``output`` so a browser or reader never sees a truncated page."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.name}.tmp")
    tmp.write_text(render(data, template), encoding="utf-8")
    os.replace(tmp, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, help="tree data JSON; see rsi-playbooks/templates/research-tree_zh.md")
    parser.add_argument("output", type=Path, help="HTML file to write, e.g. results/<task>/figures/progress/research-tree.html")
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    args = parser.parse_args()
    write(json.loads(args.data.read_text(encoding="utf-8")), args.output, args.template)


if __name__ == "__main__":
    main()

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Generate standalone webhook receivers from the shared notification template."""

from pathlib import Path


INTEGRATIONS = Path(__file__).resolve().parent


def main() -> None:
    """Regenerate the checked-in Lark and DingTalk manifests for maintainers."""
    template = (INTEGRATIONS / "webhook.yaml.tmpl").read_text(encoding="utf-8")
    envelopes = {
        "lark": '{{- dict "msg_type" "text" "content" (dict "text" $text) | toJson -}}',
        "dingtalk": '{{- dict "msgtype" "text" "text" (dict "content" $text) | toJson -}}',
    }
    for platform, envelope in envelopes.items():
        manifest = template.replace("@PLATFORM@", platform).replace("@ENVELOPE@", envelope)
        (INTEGRATIONS / platform / "alertmanagerconfig.yaml").write_text(
            manifest, encoding="utf-8"
        )


if __name__ == "__main__":
    main()

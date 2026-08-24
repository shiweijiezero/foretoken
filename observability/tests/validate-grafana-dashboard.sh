#!/usr/bin/env bash

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
dashboard="${repository_root}/deploy/charts/foretoken/files/grafana/foretoken-overview.json"

jq -e '
  [.panels[].targets[].expr] as $expressions
  | .uid == "foretoken-overview"
  and .title == "Foretoken / Overview"
  and .schemaVersion >= 39
  and (.panels | length) == 6
  and ([.panels[].id] | length) == ([.panels[].id] | unique | length)
  and ([.panels[].description | length > 0] | all)
  and ([.panels[] | select(.type == "timeseries") | .options.legend.calcs] | all(length == 0))
  and ([.panels[].datasource.uid] | all(. == "${DS_PROMETHEUS}"))
  and ([.panels[].targets[].datasource.uid] | all(. == "${DS_PROMETHEUS}"))
  and ([.templating.list[].name] | sort)
    == ["DS_PROMETHEUS", "frontend_service", "model_group", "model_role", "namespace"]
  and ($expressions | length) == 9
  and ($expressions | all(startswith("up{") or startswith("foretoken:")))
  and ($expressions | map(select(startswith("up{"))) | length) == 2
  and ($expressions | all((contains("or vector(0)")) | not))
  and ($expressions | all((test("vllm:|latency|duration|time_to_first"; "i")) | not))
  and ($expressions | any(contains("foretoken:frontend_http_response_starts:rate5m")))
  and ($expressions | any(contains("foretoken:frontend_http_response_start_5xx_ratio:rate5m")))
  and ($expressions | any(contains("foretoken:model_server_prompt_tokens:rate5m")))
  and ($expressions | any(contains("foretoken:model_server_generation_tokens:rate5m")))
  and ($expressions | any(contains("foretoken:model_server_requests_running:sum")))
  and ($expressions | any(contains("foretoken:model_server_requests_waiting:sum")))
  and ($expressions | any(contains("foretoken:model_server_kv_cache_usage_ratio:max")))
' "${dashboard}" >/dev/null

printf 'Grafana dashboard contract is valid: %s\n' "${dashboard}"

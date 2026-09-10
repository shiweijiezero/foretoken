#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
#
# SGLang sampling-parameter status sweep for the pinned engine version.
#
# Sends a fixed parameter set through foretoken -> model-server -> real SGLang
# and fails on any non-200. Catches adapter leaks of illegal parameter names or
# ranges that mocks cannot (SGLang validates in Python). The "default/unset"
# case is mandatory: ad-hoc checks that always set parameters miss it. Re-run
# after every engine version bump.
#
# Declared pin anchor (runtime check via the model-server /get_server_info
# endpoint is a manual deploy-round step):
SGLANG_PIN="sglang v0.5.18 (lmsysorg/sglang:v0.5.18)"
#
# Usage (run from the repo root or data-plane/):
#   export FRONTEND_URL=http://<lb>:8080
#   export MODEL=Qwen/Qwen3-0.6B        # optional
#   data-plane/scripts/sglang-param-matrix.sh
#
# Depends on: curl. See data-plane/retesting.md for the deployment flow.

set -euo pipefail

FRONTEND_URL="${FRONTEND_URL:?set FRONTEND_URL to the deployed frontend (see data-plane/retesting.md)}"
MODEL="${MODEL:-Qwen/Qwen3-0.6B}"
CONTENT='Reply with: OK'

echo "== SGLang parameter sweep (pin: $SGLANG_PIN; model: $MODEL; endpoint: $FRONTEND_URL) =="

# Case format: label|payload suffix (key:value onward, no outer braces; the
# body template adds them).
CASES=(
  "default|\"max_tokens\":16"
  "temperature_0|\"max_tokens\":16,\"temperature\":0"
  "temperature_1.5|\"max_tokens\":16,\"temperature\":1.5"
  "top_p_half|\"max_tokens\":16,\"top_p\":0.5"
  "top_k_0_sentinel|\"max_tokens\":16,\"top_k\":0"
  "top_k_1|\"max_tokens\":16,\"top_k\":1"
  "top_k_50|\"max_tokens\":16,\"top_k\":50"
  "frequency_penalty_1|\"max_tokens\":16,\"frequency_penalty\":1.0"
  "presence_penalty_1|\"max_tokens\":16,\"presence_penalty\":1.0"
  "seed_42|\"max_tokens\":16,\"seed\":42"
  "max_tokens_1|\"max_tokens\":1"
  "stop_token_ids|\"max_tokens\":16,\"stop_token_ids\":[151645]"
  "combined|\"max_tokens\":16,\"temperature\":0.7,\"top_k\":40,\"seed\":42"
)

# $1=payload suffix  $2=output file; prints HTTP code (000 on curl failure).
request_sglang() {
  local body
  body="{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$CONTENT\"}],$1}"
  curl -sS --max-time 90 -o "$2" -w '%{http_code}' \
    "$FRONTEND_URL/v1/chat/completions" \
    -H 'Content-Type: application/json' -d "$body"
}

fail=0
printf 'case\thttp\tresult\tresponse-summary\n'
for entry in "${CASES[@]}"; do
  label="${entry%%|*}"
  suffix="${entry#*|}"
  resp_file=$(mktemp) || exit 1
  code=$(request_sglang "$suffix" "$resp_file")
  summary=$(head -c 160 "$resp_file" | tr '\n' ' ')
  rm -f "$resp_file"
  if [ "$code" = "200" ]; then
    result=PASS
  else
    result=FAIL
    fail=$((fail + 1))
  fi
  printf '%s\t%s\t%s\t%s\n' "$label" "$code" "$result" "$summary"
done

echo "----"
if [ "$fail" -eq 0 ]; then
  echo "Sweep passed: parameter face verified for $SGLANG_PIN."
else
  echo "Sweep failed $fail case(s): check the adapter mapping first, then the engine-version semantics."
  exit 1
fi

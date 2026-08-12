#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_link="$root/third_party/vllm-rust/source"
local_source=${FORETOKEN_VLLM_SOURCE:-}

valid_source() {
    [[ -f "$1/rust/Cargo.toml" ]] \
        && git -C "$1" rev-parse --verify HEAD >/dev/null 2>&1
}

if [[ -n "$local_source" ]]; then
    if ! valid_source "$local_source"; then
        printf 'vLLM source: %s must be a Git checkout containing rust/Cargo.toml\n' "$local_source" >&2
        exit 1
    fi

    local_source=$(cd "$local_source" && pwd -P)
    if [[ -e "$source_link" || -L "$source_link" ]]; then
        if [[ ! -L "$source_link" ]]; then
            printf 'vLLM source: %s is a directory; replace it manually with a symlink\n' "$source_link" >&2
            exit 1
        fi
        rm "$source_link"
    fi
    ln -s "$local_source" "$source_link"
    printf 'vLLM source: using local build source %s\n' "$local_source"
    exit 0
fi

if valid_source "$source_link"; then
    printf 'vLLM source: using %s\n' "$(cd "$source_link" && pwd -P)"
    exit 0
fi

printf 'vLLM source: set FORETOKEN_VLLM_SOURCE to a checkout containing rust/Cargo.toml\n' >&2
exit 1

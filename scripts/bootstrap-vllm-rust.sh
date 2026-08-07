#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
#
# Materialize the pinned vLLM source used by the Rust data plane. This script
# never resets or overwrites a source tree whose state differs from the lock.

set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
lock="$root/third_party/vllm-rust/source.lock.toml"
source_dir="$root/reference/upstream/vllm"

lock_value() {
    local key=$1
    grep -E "^${key} = " "$lock" | cut -d '"' -f 2
}

repository=$(lock_value repository)
commit=$(lock_value commit)
patch_path=$(lock_value patch)
patch_sha256=$(lock_value patch_sha256)
patch="$root/$patch_path"

actual_patch_sha256=$(shasum -a 256 "$patch" | cut -d ' ' -f 1)
if [[ "$actual_patch_sha256" != "$patch_sha256" ]]; then
    printf 'vLLM bootstrap: patch checksum does not match %s\n' "$lock" >&2
    exit 1
fi

if [[ ! -d "$source_dir/.git" ]]; then
    if [[ -e "$source_dir" ]]; then
        printf 'vLLM bootstrap: %s exists but is not a git worktree; refusing to replace it\n' "$source_dir" >&2
        exit 1
    fi

    mkdir -p "$(dirname "$source_dir")"
    git clone --filter=blob:none "$repository" "$source_dir"
fi

actual_repository=$(git -C "$source_dir" remote get-url origin 2>/dev/null || true)
if [[ "$actual_repository" != "$repository" ]]; then
    printf 'vLLM bootstrap: origin is %s, expected %s; refusing to modify it\n' "$actual_repository" "$repository" >&2
    exit 1
fi

actual_commit=$(git -C "$source_dir" rev-parse HEAD)
if [[ "$actual_commit" != "$commit" ]]; then
    if [[ -z "$(git -C "$source_dir" status --porcelain --untracked-files=all)" ]]; then
        git -C "$source_dir" fetch --depth=1 origin "$commit"
        git -C "$source_dir" checkout --detach "$commit"
    else
        printf 'vLLM bootstrap: source is at %s, expected %s, with local changes; refusing to reset it\n' "$actual_commit" "$commit" >&2
        exit 1
    fi
fi

if git -C "$source_dir" diff --quiet HEAD && git -C "$source_dir" diff --cached --quiet; then
    if [[ -n "$(git -C "$source_dir" ls-files --others --exclude-standard)" ]]; then
        printf 'vLLM bootstrap: source has untracked files; refusing to modify it\n' >&2
        exit 1
    fi
    git -C "$source_dir" apply --check "$patch"
    git -C "$source_dir" apply "$patch"
    printf 'vLLM bootstrap: applied %s at %s\n' "$patch_path" "$commit"
    exit 0
fi

expected_patch_id=$(git patch-id --stable < "$patch" | cut -d ' ' -f 1)
actual_patch_id=$(git -C "$source_dir" diff --binary --full-index HEAD | git patch-id --stable | cut -d ' ' -f 1)
changed_files=$(git -C "$source_dir" diff --name-only HEAD)
if [[ "$expected_patch_id" == "$actual_patch_id" ]] \
    && [[ "$changed_files" == "rust/src/chat/src/lib.rs" ]] \
    && git -C "$source_dir" diff --cached --quiet \
    && git -C "$source_dir" apply --reverse --check "$patch"; then
    printf 'vLLM bootstrap: %s is already applied at %s\n' "$patch_path" "$commit"
    exit 0
fi

printf 'vLLM bootstrap: source has changes outside the pinned patch; refusing to modify it\n' >&2
exit 1

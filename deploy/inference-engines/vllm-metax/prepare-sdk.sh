#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Prepare one SDK image layer, retaining completed downloads if a later layer fails.
set -euo pipefail

stage=$1
prefix=$2
installer_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
sdk_info=$(python3 -c \
  'import json, sys; s=json.load(open(sys.argv[1])); a=s["torchaudio"]; print(s["maca"], s["torch"], a["repository"], a["revision"], a["version"])' \
  "$installer_dir/sdk-environment.json")
read -r maca_version torch_version audio_repository audio_revision audio_version <<< "$sdk_info"
export UV_EXTRA_INDEX_URL=${UV_EXTRA_INDEX_URL:-https://repos.metax-tech.com/r/maca-pypi/simple}
export UV_INDEX_STRATEGY=unsafe-best-match
python="$prefix/.venv/bin/python"

case "$stage" in
  packages)
    curl --fail --location https://repos.metax-tech.com/public.gpg.key \
      | gpg --batch --dearmor --output /usr/share/keyrings/metax.gpg
    printf '%s\n' \
      'deb [signed-by=/usr/share/keyrings/metax.gpg] https://repos.metax-tech.com/r/maca-sdk-deb/ stable main' \
      > /etc/apt/sources.list.d/metax.list
    apt-get update
    # PyTorch also links mctlassEx, which the SDK meta-package does not install.
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      "maca_sdk=$maca_version" "mctlassex_${maca_version%.*}=$maca_version"
    rm -rf /var/lib/apt/lists/*
    ;;
  torch)
    mkdir "$prefix"
    uv venv "$prefix/.venv" --python /usr/bin/python3
    uv pip install --python "$python" setuptools wheel numpy "torch==$torch_version"
    ;;
  audio)
    # Compile CPU audio extensions against the installed Torch ABI.
    build_dir=$(mktemp -d)
    github_base=${FORETOKEN_GITHUB_MIRROR:-https://github.com}
    curl --fail --location --output "$build_dir/audio.tar.gz" \
      "${github_base%/}/$audio_repository/archive/$audio_revision.tar.gz"
    mkdir "$build_dir/audio"
    tar --extract --gzip --strip-components=1 \
      --file "$build_dir/audio.tar.gz" --directory "$build_dir/audio"
    "$python" "$installer_dir/torch_cpu_config.py" "$build_dir/torch-cpu"
    Torch_ROOT="$build_dir/torch-cpu" USE_CUDA=0 USE_ROCM=0 BUILD_CUDA_CTC_DECODER=0 \
    BUILD_VERSION="$audio_version" PYTORCH_VERSION="$torch_version" \
    CMAKE_BUILD_PARALLEL_LEVEL="${BUILD_JOBS:-2}" \
    uv build --python "$python" --wheel --no-build-isolation \
      --out-dir "$build_dir/wheels" "$build_dir/audio"
    uv pip install --python "$python" --no-deps "$build_dir"/wheels/torchaudio-*.whl
    install -Dm644 "$build_dir/audio/LICENSE" /usr/local/share/licenses/torchaudio/LICENSE
    rm -rf "$build_dir"
    uv pip check --python "$python"
    ;;
  *)
    printf 'Unknown SDK build stage: %s\n' "$stage" >&2
    exit 2
    ;;
esac

#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Create an isolated vLLM Python environment on a Linux host or build container with the MACA SDK.
set -euo pipefail

if [[ $# != 1 ]]; then
  printf 'Usage: bash %s INSTALL_DIRECTORY\n' "$0" >&2
  exit 2
fi

prefix=$1
maca_path=${MACA_PATH:-/opt/maca}
installer_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source_pair="$installer_dir/patches/vllm-030/glm-5.3/source-environment.json"

# Create a new installation directory without overwriting existing environments or source trees.
mkdir -p "$(dirname "$prefix")"
mkdir "$prefix"
prefix=$(cd "$prefix" && pwd)
mkdir "$prefix/third_party"
sdk_python=${UV_PYTHON:-}
if [[ -z "$sdk_python" ]]; then
  # SDK images may keep their Python outside PATH; reuse only the matching
  # distribution source, not its environment as the serving runtime.
  for candidate in /opt/conda/bin/python /usr/local/bin/python3 /usr/bin/python3; do
    if [[ -x "$candidate" ]] && "$candidate" -c \
      'from importlib.metadata import version; version("torch"); version("torchaudio")' \
      >/dev/null 2>&1; then
      sdk_python=$candidate
      break
    fi
  done
  if [[ -z "$sdk_python" ]]; then
    printf '%s\n' 'This MetaX build requires a MACA/PyTorch SDK image containing its matching torchaudio distribution.' >&2
    exit 1
  fi
fi
uv venv "$prefix/.venv" --python "$sdk_python"
python="$prefix/.venv/bin/python"

# The source pair owns the core and plugin revisions, versions, and ordered patches.
for project in vllm-metax vllm; do
  if [[ "$project" == vllm-metax ]]; then
    target=metax
  else
    target=core
  fi
  source_info=$("$python" -c \
    'import json, sys; s=json.load(open(sys.argv[1]))[sys.argv[2]]; print(s["repository"], s["revision"], s["version"])' \
    "$source_pair" "$target")
  read -r repository revision package_version <<< "$source_info"
  if [[ "$target" == metax ]]; then
    plugin_version=$package_version
  else
    core_version=$package_version
  fi
  source_dir="$prefix/third_party/$project"
  mkdir "$source_dir"
  github_base=${FORETOKEN_GITHUB_MIRROR:-https://github.com}
  curl --fail --location --output "$prefix/third_party/$project.tar.gz" \
    "${github_base%/}/$repository/archive/$revision.tar.gz"
  tar --extract --gzip --strip-components=1 \
    --file "$prefix/third_party/$project.tar.gz" --directory "$source_dir"
  rm "$prefix/third_party/$project.tar.gz"
done

# Apply each patch at the source or installed-package stage that owns its files.
apply_source_pair_patches() {
  local target=$1 directory=$2 patches patch_file
  patches=$("$python" -c \
    'import json, sys; print("\n".join(json.load(open(sys.argv[1]))["patches"][sys.argv[2]]))' \
    "$source_pair" "$target")
  while IFS= read -r patch_file; do
    patch --batch --forward --fuzz=0 --strip=1 --directory "$directory" \
      < "$(dirname "$source_pair")/$patch_file"
  done <<< "$patches"
}

apply_source_pair_patches metax "$prefix/third_party/vllm-metax"
apply_source_pair_patches core "$prefix/third_party/vllm"
export USE_PRECOMPILED_KERNEL=1

# The vendor script configures compilers and shared libraries; activation also supports running vLLM.
{
  printf 'export LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}\n'
  printf 'source %q %q\n' "$prefix/third_party/vllm-metax/env.sh" "$maca_path"
  printf 'export CPATH=%q${CPATH:+:$CPATH}\n' "$maca_path/include:$maca_path/include/mcr"
  printf 'export CUBRIDGE_HOME=%q\n' "$prefix"
  printf 'export CUDA_PATH=%q\n' "$prefix/cu-bridge/CUDA_DIR"
  printf 'source %q\n' "$prefix/.venv/bin/activate"
} > "$prefix/activate"
# shellcheck source=/dev/null
source "$prefix/activate"

export UV_EXTRA_INDEX_URL=${UV_EXTRA_INDEX_URL:-https://repos.metax-tech.com/r/maca-pypi/simple}
export UV_INDEX_STRATEGY=unsafe-best-match
uv pip install --python "$python" \
  -r "$prefix/third_party/vllm-metax/requirements/build.txt"
"$python" "$installer_dir/sdk_audio.py" "$prefix"

# Build the MetaX plugin for the CUDA-compatible target; upstream supplies only the Python layer.
# Build the plugin wheel first, then resolve it with upstream source to keep build environments separate.
SETUPTOOLS_SCM_PRETEND_VERSION="$plugin_version" VLLM_TARGET_DEVICE=cuda \
uv build --python "$python" --wheel --no-build-isolation \
  --out-dir "$prefix/wheels" "$prefix/third_party/vllm-metax"
VLLM_VERSION_OVERRIDE="$core_version" VLLM_TARGET_DEVICE=empty \
uv pip install --python "$python" --no-build-isolation \
  "$prefix"/sdk-wheels/*.whl "$prefix"/wheels/vllm_metax-*.whl "$prefix/third_party/vllm"
site_packages=$("$python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
apply_source_pair_patches installed "$site_packages"
uv pip check --python "$python"
printf 'Activate with: source %q\n' "$prefix/activate"

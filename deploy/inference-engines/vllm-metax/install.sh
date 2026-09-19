#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Create an isolated vLLM Python environment on a Linux host or build container with the MACA SDK.
set -euo pipefail

if [[ $# != 2 ]]; then
  printf 'Usage: bash %s INSTALL_DIRECTORY VLLM_VERSION\n' "$0" >&2
  exit 2
fi

prefix=$1
version=$2
maca_path=${MACA_PATH:-/opt/maca}

# Create a new installation directory without overwriting existing environments or source trees.
mkdir -p "$(dirname "$prefix")"
mkdir "$prefix"
prefix=$(cd "$prefix" && pwd)
mkdir "$prefix/third_party"
sdk_python=${UV_PYTHON:-}
if [[ "$version" == 0.26.0 && -z "$sdk_python" ]]; then
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
    printf '%s\n' 'MetaX 0.26 requires a MACA/PyTorch SDK image containing its matching torchaudio distribution.' >&2
    exit 1
  fi
fi
uv venv "$prefix/.venv" --python "${sdk_python:-3.12}"
python="$prefix/.venv/bin/python"

for project in vllm-metax vllm; do
  if [[ "$project" == vllm-metax ]]; then
    repository=MetaX-MACA/vLLM-metax
  else
    repository=vllm-project/vllm
  fi
  source_dir="$prefix/third_party/$project"
  mkdir "$source_dir"
  github_base=${FORETOKEN_GITHUB_MIRROR:-https://github.com}
  curl --fail --location --output "$prefix/third_party/$project.tar.gz" \
    "${github_base%/}/$repository/archive/refs/tags/v$version.tar.gz"
  tar --extract --gzip --strip-components=1 \
    --file "$prefix/third_party/$project.tar.gz" --directory "$source_dir"
  rm "$prefix/third_party/$project.tar.gz"
done

plugin_version=$version
constraints=()
installer_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ "$version" == 0.24.0 ]]; then
  patch --directory "$prefix/third_party/vllm-metax" --strip=1 \
    < "$installer_dir/xgrammar-0.24.patch"
  plugin_version=0.24.0+foretoken.1
  constraints=(--constraint "$installer_dir/constraints-0.24.txt")
elif [[ "$version" == 0.26.0 ]]; then
  patch --directory "$prefix/third_party/vllm-metax" --strip=1 \
    < "$installer_dir/dependencies-0.26.patch"
  plugin_version=0.26.0+foretoken.1
fi

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

sdk_packages=()
if [[ "$version" == 0.26.0 ]]; then
  "$python" "$installer_dir/sdk_audio.py" "$prefix"
  sdk_packages=("$prefix"/sdk-wheels/*.whl)
fi

# Build the MetaX plugin for the CUDA-compatible target; upstream supplies only the Python layer.
# Build the plugin wheel first, then resolve it with upstream source to keep build environments separate.
SETUPTOOLS_SCM_PRETEND_VERSION="$plugin_version" VLLM_TARGET_DEVICE=cuda \
uv build --python "$python" --wheel --no-build-isolation \
  --out-dir "$prefix/wheels" "$prefix/third_party/vllm-metax"
VLLM_VERSION_OVERRIDE="$version" VLLM_TARGET_DEVICE=empty \
uv pip install --python "$python" --no-build-isolation "${constraints[@]}" \
  "${sdk_packages[@]}" "$prefix"/wheels/vllm_metax-*.whl "$prefix/third_party/vllm"
uv pip check --python "$python"
printf 'Activate with: source %q\n' "$prefix/activate"

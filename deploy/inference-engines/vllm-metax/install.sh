#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# 在已有 MACA SDK 的 Linux 主机或构建容器中创建独立的 vLLM Python 环境。
set -euo pipefail

if [[ $# != 2 ]]; then
  printf 'Usage: bash %s INSTALL_DIRECTORY VLLM_VERSION\n' "$0" >&2
  exit 2
fi

prefix=$1
version=$2
maca_path=${MACA_PATH:-/opt/maca}

# 目标目录由本次安装独占，不覆盖已有环境或源码。
mkdir -p "$(dirname "$prefix")"
mkdir "$prefix"
prefix=$(cd "$prefix" && pwd)
mkdir "$prefix/third_party"
uv venv "$prefix/.venv" --python "${UV_PYTHON:-3.12}"
python="$prefix/.venv/bin/python"

for project in vllm-metax vllm; do
  if [[ "$project" == vllm-metax ]]; then
    repository=MetaX-MACA/vLLM-metax
  else
    repository=vllm-project/vllm
  fi
  source_dir="$prefix/third_party/$project"
  mkdir "$source_dir"
  curl --fail --location --output "$prefix/third_party/$project.tar.gz" \
    "https://github.com/$repository/archive/refs/tags/v$version.tar.gz"
  tar --extract --gzip --strip-components=1 \
    --file "$prefix/third_party/$project.tar.gz" --directory "$source_dir"
  rm "$prefix/third_party/$project.tar.gz"
done

plugin_version=$version
constraints=()
if [[ "$version" == 0.24.0 ]]; then
  installer_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  patch --directory "$prefix/third_party/vllm-metax" --strip=1 \
    < "$installer_dir/xgrammar-0.24.patch"
  plugin_version=0.24.0+foretoken.1
  constraints=(--constraint "$installer_dir/constraints-0.24.txt")
fi

# 官方环境脚本配置编译器与共享库；激活脚本也用于后续直接运行 vLLM。
{
  printf 'source %q %q\n' "$prefix/third_party/vllm-metax/env.sh" "$maca_path"
  printf 'export CPATH=%q${CPATH:+:$CPATH}\n' "$maca_path/include:$maca_path/include/mcr"
  printf 'export CUBRIDGE_HOME=%q\n' "$prefix"
  printf 'export CUDA_PATH=%q\n' "$prefix/cu-bridge/CUDA_DIR"
  printf 'source %q\n' "$prefix/.venv/bin/activate"
} > "$prefix/activate"
export LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}
# shellcheck source=/dev/null
source "$prefix/activate"

export UV_EXTRA_INDEX_URL=${UV_EXTRA_INDEX_URL:-https://repos.metax-tech.com/r/maca-pypi/simple}
export UV_INDEX_STRATEGY=unsafe-best-match
uv pip install --python "$python" \
  -r "$prefix/third_party/vllm-metax/requirements/build.txt"

# MetaX 插件使用 CUDA-compatible 编译目标；upstream 只提供 Python 层。
# 先构建插件 wheel，再和 upstream 源码一起求解全部运行依赖，避免共享构建环境变量。
SETUPTOOLS_SCM_PRETEND_VERSION="$plugin_version" VLLM_TARGET_DEVICE=cuda \
uv build --python "$python" --wheel --no-build-isolation \
  --out-dir "$prefix/wheels" "$prefix/third_party/vllm-metax"
VLLM_VERSION_OVERRIDE="$version" VLLM_TARGET_DEVICE=empty \
uv pip install --python "$python" --no-build-isolation "${constraints[@]}" \
  "$prefix"/wheels/vllm_metax-*.whl "$prefix/third_party/vllm"
uv pip check --python "$python"
printf 'Activate with: source %q\n' "$prefix/activate"

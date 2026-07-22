#!/bin/bash
set -euo pipefail

# Resolve paths relative to this script so it works from any current directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TOOL_VERSIONS_FILE="${TOOL_VERSIONS_FILE:-${SCRIPT_DIR}/tool-versions.env}"

# Load repo-pinned tool defaults without overriding explicit environment values.
load_tool_versions() {
  if [ ! -f "${TOOL_VERSIONS_FILE}" ]; then
    echo "ERROR: Tool versions file not found: ${TOOL_VERSIONS_FILE}" >&2
    return 1
  fi

  local key value
  while IFS='=' read -r key value || [ -n "${key}" ]; do
    key="${key%%[[:space:]]*}"
    case "${key}" in
      ''|'#'*)
        continue
        ;;
    esac

    if [[ ! "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      echo "ERROR: Invalid variable name in ${TOOL_VERSIONS_FILE}: ${key}" >&2
      return 1
    fi

    value="${value%%#*}"
    value="${value%"${value##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"

    if [ -z "${!key+x}" ]; then
      printf -v "${key}" '%s' "${value}"
    fi
  done < "${TOOL_VERSIONS_FILE}"
}

# User-local install/cache paths. LOCALBIN is provided by devcontainer.json in
# normal runs, while these fallbacks keep the script usable when run manually.
BIN_DIR="${LOCALBIN:-${HOME}/.cache/foretoken/control-plane-bin}"
BASH_COMPLETION_ROOT="${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/bash-completion}"
BASH_COMPLETIONS_DIR="${BASH_COMPLETION_ROOT}/completions"
HISTFILE="${HISTFILE:-${HOME}/.commandhistory}"
HISTFILE_DIR="${HISTFILE%/*}"
FAILED_INSTALLS=()

# Network defaults. Keep URL-containing defaults in shell rather than
# devcontainer.json because Dev Container interpolation treats ':' specially.
DEFAULT_NO_PROXY="localhost,127.0.0.1,host.docker.internal,.svc,.cluster.local,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
DEFAULT_GOPROXY="https://proxy.golang.org,direct"
DEFAULT_GOSUMDB="sum.golang.org"

# Rewrite host-local proxy endpoints for Docker Desktop containers. Inside the
# container, localhost/127.0.0.1 is the container itself; host.docker.internal is
# the route back to the host machine.
rewrite_host_local_proxy_url() {
  local value="$1"

  value="${value//:\/\/localhost/:\/\/host.docker.internal}"
  value="${value//:\/\/127.0.0.1/:\/\/host.docker.internal}"
  value="${value//:\/\/[::1]/:\/\/host.docker.internal}"

  case "${value}" in
    localhost:*)
      value="host.docker.internal:${value#localhost:}"
      ;;
    127.0.0.1:*)
      value="host.docker.internal:${value#127.0.0.1:}"
      ;;
    '[::1]':*)
      value="host.docker.internal:${value#'[::1]':}"
      ;;
  esac

  printf '%s\n' "${value}"
}

# Best-effort redaction for diagnostics. Avoid printing credentials from proxy
# URLs such as http://user:pass@host:port.
sanitize_proxy_url() {
  local value="$1"

  if [ -z "${value}" ]; then
    printf '<unset>\n'
    return 0
  fi

  if [[ "${value}" =~ ^([^:/]+://)([^/@]+@)(.*)$ ]]; then
    printf '%s***@%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[3]}"
  else
    printf '%s\n' "${value}"
  fi
}

normalize_proxy_env() {
  # 1. Choose canonical values from either uppercase or lowercase inputs.
  local http="${HTTP_PROXY:-${http_proxy:-}}"
  local https="${HTTPS_PROXY:-${https_proxy:-}}"
  local all="${ALL_PROXY:-${all_proxy:-}}"
  local no="${NO_PROXY:-${no_proxy:-${DEFAULT_NO_PROXY}}}"

  # 2. Rewrite host-local proxy endpoints for container use.
  http="$(rewrite_host_local_proxy_url "${http}")"
  https="$(rewrite_host_local_proxy_url "${https}")"
  all="$(rewrite_host_local_proxy_url "${all}")"

  # 3. Export canonical values in both cases. Force lowercase variants to the
  # same canonical values.
  # curl may prefer lowercase variables, so preserving stale lowercase
  # localhost values can bypass the rewrite and point back at the container.
  export HTTP_PROXY="${http}"
  export HTTPS_PROXY="${https}"
  export ALL_PROXY="${all}"
  export NO_PROXY="${no}"
  export http_proxy="${http}"
  export https_proxy="${https}"
  export all_proxy="${all}"
  export no_proxy="${no}"
}

print_proxy_diagnostics() {
  echo "Proxy diagnostics after normalization (sanitized):"
  echo "  HTTP_PROXY=$(sanitize_proxy_url "${HTTP_PROXY:-}")"
  echo "  HTTPS_PROXY=$(sanitize_proxy_url "${HTTPS_PROXY:-}")"
  echo "  ALL_PROXY=$(sanitize_proxy_url "${ALL_PROXY:-}")"
  echo "  NO_PROXY=${NO_PROXY:-<unset>}"
}

# Go network defaults. Keep these in shell instead of devcontainer.json because
# Dev Container localEnv interpolation treats ':' specially.
normalize_go_env() {
  export GOPROXY="${GOPROXY:-${DEFAULT_GOPROXY}}"
  export GOSUMDB="${GOSUMDB:-${DEFAULT_GOSUMDB}}"
}

configure_network_env() {
  normalize_proxy_env
  normalize_go_env
  print_proxy_diagnostics
}

echo "===================================="
echo "Foretoken Control Plane DevContainer Setup"
echo "===================================="

# Keep all generated files user-owned. Running as root commonly leaves caches,
# shell history, and tool directories unwritable by the remote user.
if [ "$(id -u)" -eq 0 ]; then
  echo "ERROR: This script should run as the non-root remote user, not root." >&2
  echo "Current user: $(whoami) (UID: $(id -u))" >&2
  exit 1
fi

echo "Running post-install script as user: $(whoami) (UID: $(id -u))"

# Optional regional apt mirror. It is deliberately opt-in so global teams are
# not forced onto a mirror that may be slower or inaccessible elsewhere.
configure_apt_mirror() {
  case "${USE_TUNA_APT_MIRROR}" in
    true|1|yes|on)
      ;;
    *)
      echo "Skipping TUNA apt mirror setup. Set USE_TUNA_APT_MIRROR=true before rebuilding to enable it."
      return 0
      ;;
  esac

  if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
    echo "WARNING: sudo is not available; cannot configure apt mirror"
    return 0
  fi

  local sudo_cmd=()
  if [ "$(id -u)" -ne 0 ]; then
    sudo_cmd=(sudo)
  fi

  local changed=false
  echo "Configuring Debian apt sources to use TUNA mirror..."

  if [ -f /etc/apt/sources.list.d/debian.sources ]; then
    "${sudo_cmd[@]}" sed -i \
      -e 's|https://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
      -e 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
      -e 's|https://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
      -e 's|http://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
      /etc/apt/sources.list.d/debian.sources
    changed=true
  fi

  if [ -f /etc/apt/sources.list ]; then
    "${sudo_cmd[@]}" sed -i \
      -e 's|https://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
      -e 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
      -e 's|https://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
      -e 's|http://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
      /etc/apt/sources.list
    changed=true
  fi

  if [ "${changed}" = true ]; then
    echo "Apt sources configured for TUNA. Run 'sudo apt-get update' before installing apt packages."
  else
    echo "WARNING: Debian apt source files not found; skipped TUNA apt mirror setup"
  fi
}

# Map kernel architecture names to release-asset architecture names.
detect_arch() {
  case "$(uname -m)" in
    x86_64)
      echo "amd64"
      ;;
    aarch64|arm64)
      echo "arm64"
      ;;
    *)
      echo "ERROR: Unsupported architecture: $(uname -m)" >&2
      return 1
      ;;
  esac
}

# Append a shell startup line only once; devcontainer lifecycle hooks may be
# rerun, so all shell customization must be idempotent.
ensure_bashrc_line() {
  local line="$1"
  local bashrc="${HOME}/.bashrc"

  touch "${bashrc}"
  if ! grep -qxF "${line}" "${bashrc}"; then
    printf '%s\n' "${line}" >> "${bashrc}"
  fi
}

# Replace a named .bashrc block on every run. Use this for values that can
# change between rebuilds, such as normalized proxy variables from localEnv.
write_bashrc_managed_block() {
  local name="$1"
  local content="$2"
  local bashrc="${HOME}/.bashrc"
  local begin="# >>> foretoken ${name} >>>"
  local end="# <<< foretoken ${name} <<<"
  local tmp="${bashrc}.tmp.$$"
  local in_block=false

  touch "${bashrc}"
  : > "${tmp}"

  while IFS= read -r line || [ -n "${line}" ]; do
    if [ "${line}" = "${begin}" ]; then
      in_block=true
      continue
    fi
    if [ "${line}" = "${end}" ]; then
      in_block=false
      continue
    fi
    if [ "${in_block}" != true ]; then
      printf '%s\n' "${line}" >> "${tmp}"
    fi
  done < "${bashrc}"

  if [ -n "${content}" ]; then
    {
      printf '%s\n' "${begin}"
      printf '%s' "${content}"
      printf '%s\n' "${end}"
    } >> "${tmp}"
  fi

  mv "${tmp}" "${bashrc}"
}

configure_shell_proxy_env() {
  local content=""
  local name value

  for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy; do
    value="${!name:-}"
    if [ -n "${value}" ]; then
      content+="$(printf 'export %s=%q' "${name}" "${value}")"
      content+=$'\n'
    fi
  done

  write_bashrc_managed_block "proxy-env" "${content}"
}

ensure_go_user_dirs() {
  # Go module/build caches are Docker volumes. Old containers or root-run setup
  # attempts can leave them root-owned, which breaks `go install` and tests.
  ensure_user_writable_dir "/go/pkg/mod"
  ensure_user_writable_dir "/go/pkg/mod/cache"
  ensure_user_writable_dir "/go/pkg/sumdb"
  ensure_user_writable_dir "${HOME}/.cache/go-build"
}

# Ensure mounted Docker volumes or cache paths are writable by the remote user.
ensure_user_writable_dir() {
  local dir="$1"

  mkdir -p "${dir}" 2>/dev/null || true
  if [ -d "${dir}" ] && [ -w "${dir}" ]; then
    return 0
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: Cannot create ${dir}, and sudo is not available" >&2
    return 1
  fi

  sudo mkdir -p "${dir}"
  sudo chown -R "$(id -u):$(id -g)" "${dir}"
}

# Ensure a file exists and is writable by the remote user.
ensure_user_writable_file() {
  local file="$1"

  touch "${file}" 2>/dev/null && return 0

  if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: Cannot create ${file}, and sudo is not available" >&2
    return 1
  fi

  sudo touch "${file}"
  sudo chown "$(id -u):$(id -g)" "${file}"
}

# Optionally verify release binaries with SHA256 values from the environment,
# e.g. KUBEBUILDER_SHA256. Empty checksum variables skip this check.
verify_binary_checksum() {
  local name="$1"
  local file="$2"
  local var_name
  var_name="$(printf '%s_SHA256' "${name}" | tr '[:lower:]-' '[:upper:]_')"
  local expected="${!var_name:-}"

  if [ -z "${expected}" ]; then
    return 0
  fi

  if ! command -v sha256sum >/dev/null 2>&1; then
    echo "ERROR: ${var_name} is set, but sha256sum is not available" >&2
    return 1
  fi

  local actual
  actual="$(sha256sum "${file}" | cut -d ' ' -f 1)"
  if [ "${actual}" != "${expected}" ]; then
    echo "ERROR: ${name} checksum mismatch: expected ${expected}, got ${actual}" >&2
    return 1
  fi
}

# Verify that a downloaded binary reports the version configured in
# tool-versions.env, preventing silent drift to a latest/default release.
verify_binary_version() {
  local name="$1"
  local version="$2"
  local binary="$3"
  local output

  case "${name}" in
    kind)
      output="$("${binary}" version 2>&1 || true)"
      ;;
    kubebuilder)
      output="$("${binary}" version 2>&1 || true)"
      ;;
    kubectl)
      output="$("${binary}" version --client 2>&1 || true)"
      ;;
    *)
      return 0
      ;;
  esac

  if [[ "${output}" != *"${version}"* ]]; then
    echo "ERROR: ${name} version mismatch: expected ${version}, got: ${output}" >&2
    return 1
  fi
}

# Install single-file release binaries as versioned targets plus stable symlinks,
# e.g. kubectl-v1.35.0 and kubectl -> kubectl-v1.35.0. This makes active versions visible
# while preserving stable command names on PATH.
install_binary() {
  local name="$1"
  local version="$2"
  shift 2
  local target="${BIN_DIR}/${name}-${version}"
  local link="${BIN_DIR}/${name}"
  local tmp="${target}.download"

  if [ -x "${target}" ] && verify_binary_version "${name}" "${version}" "${target}"; then
    ln -sf "${target}" "${link}"
    echo "${name} ${version} already installed in ${target}"
    return 0
  fi

  echo "Installing ${name} ${version}..."
  rm -f "${tmp}"
  local url
  for url in "$@"; do
    echo "Downloading ${name} from ${url}"
    if curl --fail --show-error --silent --location \
      --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 20 \
      --max-time "${DOWNLOAD_MAX_TIME_SECONDS}" \
      --speed-limit "${DOWNLOAD_MIN_SPEED_BYTES}" --speed-time "${DOWNLOAD_MIN_SPEED_TIME_SECONDS}" \
      -o "${tmp}" "${url}"; then
      verify_binary_checksum "${name}" "${tmp}" || {
        rm -f "${tmp}"
        echo "WARNING: Failed to verify ${name} downloaded from ${url}" >&2
        continue
      }
      mv "${tmp}" "${target}"
      chmod +x "${target}"
      if ! verify_binary_version "${name}" "${version}" "${target}"; then
        rm -f "${target}"
        echo "WARNING: Failed to verify ${name} ${version} downloaded from ${url}" >&2
        continue
      fi
      ln -sf "${target}" "${link}"
      echo "${name} installed in ${target}"
      return 0
    fi

    rm -f "${tmp}"
    echo "WARNING: Failed to download ${name} from ${url}" >&2
  done

  echo "ERROR: Failed to install ${name} ${version}" >&2
  return 1
}

# Generate shell completions into the user's completion directory. Failure is a
# convenience warning, not a reason to break the devcontainer.
install_completion() {
  local name="$1"
  shift

  if "$@" > "${BASH_COMPLETIONS_DIR}/${name}" 2>/dev/null; then
    echo "${name} completion installed"
  else
    rm -f "${BASH_COMPLETIONS_DIR}/${name}"
    echo "WARNING: Failed to generate ${name} completion"
  fi
}

# Rust toolchain. rust-toolchain.toml is the repo source of truth; RUST_TOOLCHAIN
# is only an explicit local override for testing a different toolchain.
rust_is_installed() {
  command -v rustc >/dev/null 2>&1 && command -v cargo >/dev/null 2>&1
}

read_rust_toolchain() {
  local toolchain_file="${REPO_ROOT}/rust-toolchain.toml"
  local channel=""

  if [ -f "${toolchain_file}" ]; then
    channel="$(sed -n 's/^[[:space:]]*channel[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${toolchain_file}" | head -n 1)"
  fi

  if [ -z "${channel}" ]; then
    echo "ERROR: Rust toolchain channel not found in ${toolchain_file}" >&2
    return 1
  fi

  printf '%s\n' "${channel}"
}

# rustup's direct binary fallback uses Rust target triples, not Docker asset
# names. Keep this separate from detect_arch so both mappings stay obvious.
detect_rustup_arch() {
  case "$(uname -m)" in
    x86_64)
      echo "x86_64-unknown-linux-gnu"
      ;;
    aarch64|arm64)
      echo "aarch64-unknown-linux-gnu"
      ;;
    *)
      echo "ERROR: Unsupported rustup architecture: $(uname -m)" >&2
      return 1
      ;;
  esac
}

rustup_direct_url() {
  local rustup_arch="$1"
  printf '%s/%s/rustup-init\n' "${RUSTUP_DIRECT_BASE_URL}" "${rustup_arch}"
}

ensure_rust_user_dirs() {
  # Old containers or root-run setup attempts can leave Rust directories
  # root-owned. Fix them before rustup tries to create ~/.cargo/bin.
  ensure_user_writable_dir "${HOME}/.cargo"
  ensure_user_writable_dir "${HOME}/.cargo/bin"
  ensure_user_writable_dir "${HOME}/.rustup"
}

# Install Rust with rustup only when rustc/cargo are missing. The shell installer
# is tried first, then the official static rustup-init binary is used as a
# fallback when sh.rustup.rs fails under proxy/TLS rules.
install_rust() {
  if rust_is_installed; then
    echo "Rust already installed: $(rustc --version)"
    return 0
  fi

  ensure_rust_user_dirs

  echo "Installing Rust toolchain ${RUST_TOOLCHAIN} with rustup..."
  local shell_installer="${BIN_DIR}/rustup-init.sh"
  local binary_installer="${BIN_DIR}/rustup-init"
  local direct_url
  direct_url="$(rustup_direct_url "$(detect_rustup_arch)")"

  rm -f "${shell_installer}" "${binary_installer}"
  if curl --fail --show-error --silent --location \
    --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 20 \
    --max-time "${DOWNLOAD_MAX_TIME_SECONDS}" \
    --speed-limit "${DOWNLOAD_MIN_SPEED_BYTES}" --speed-time "${DOWNLOAD_MIN_SPEED_TIME_SECONDS}" \
    -o "${shell_installer}" "${RUSTUP_INIT_URL}"; then
    sh "${shell_installer}" -y --default-toolchain "${RUST_TOOLCHAIN}" --profile default
    rm -f "${shell_installer}"
  else
    rm -f "${shell_installer}"
    echo "WARNING: Failed to download rustup shell installer from ${RUSTUP_INIT_URL}" >&2
    echo "Trying direct rustup-init binary fallback..."

    if curl --fail --show-error --silent --location \
      --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 20 \
      --max-time "${DOWNLOAD_MAX_TIME_SECONDS}" \
      --speed-limit "${DOWNLOAD_MIN_SPEED_BYTES}" --speed-time "${DOWNLOAD_MIN_SPEED_TIME_SECONDS}" \
      -o "${binary_installer}" "${direct_url}"; then
      chmod +x "${binary_installer}"
      "${binary_installer}" -y --default-toolchain "${RUST_TOOLCHAIN}" --profile default
      rm -f "${binary_installer}"
    else
      rm -f "${binary_installer}"
      echo "ERROR: Failed to download rustup from both ${RUSTUP_INIT_URL} and ${direct_url}" >&2
      return 1
    fi
  fi

  export PATH="${HOME}/.cargo/bin:${PATH}"
  rustc --version
  cargo --version
}

# Run optional installers without making container creation fail on transient
# network/proxy problems. Failures are summarized at the end with rerun guidance.
install_or_warn() {
  local name="$1"
  shift

  if "$@"; then
    return 0
  fi

  FAILED_INSTALLS+=("${name}")
  echo "WARNING: ${name} was not installed. Check proxy/network settings and rerun /workspace/.devcontainer/post-install.sh when connectivity is available." >&2
  return 0
}

# Run a version check when a command exists; otherwise print a warning.
verify_command() {
  local name="$1"
  shift

  if command -v "${name}" >/dev/null 2>&1; then
    "$@"
  else
    echo "WARNING: ${name} is not installed"
  fi
}

load_tool_versions
RUST_TOOLCHAIN="${RUST_TOOLCHAIN:-$(read_rust_toolchain)}"

configure_network_env
configure_apt_mirror

ARCH="$(detect_arch)"
echo "Architecture: ${ARCH}"

ensure_user_writable_dir "${BIN_DIR}"
ensure_user_writable_dir "${BASH_COMPLETIONS_DIR}"
ensure_user_writable_dir "${HISTFILE_DIR}"
ensure_user_writable_file "${HISTFILE}"
ensure_go_user_dirs
export PATH="${BIN_DIR}:${PATH}"

echo ""
echo "------------------------------------"
echo "Configuring shell environment..."
echo "------------------------------------"

ensure_bashrc_line "export LOCALBIN=\"${BIN_DIR}\""
ensure_bashrc_line "export PATH=\"${BIN_DIR}:\${PATH}\""
ensure_bashrc_line "export BASH_COMPLETION_USER_DIR=\"${BASH_COMPLETION_ROOT}\""
ensure_bashrc_line "export HISTFILE=\"${HISTFILE}\""
ensure_bashrc_line 'export GOPROXY="${GOPROXY:-https://proxy.golang.org,direct}"'
ensure_bashrc_line 'export GOSUMDB="${GOSUMDB:-sum.golang.org}"'
ensure_bashrc_line '[ -z "${HF_TOKEN:-}" ] && unset HF_TOKEN'
ensure_bashrc_line '[ -z "${GITHUB_TOKEN:-}" ] && unset GITHUB_TOKEN'
configure_shell_proxy_env

echo ""
echo "------------------------------------"
echo "Installing development tools..."
echo "------------------------------------"

install_or_warn "kubebuilder" install_binary \
  "kubebuilder" \
  "${KUBEBUILDER_VERSION}" \
  "https://go.kubebuilder.io/dl/${KUBEBUILDER_VERSION}/linux/${ARCH}" \
  "${KUBEBUILDER_GITHUB_BASE_URL}/v${KUBEBUILDER_VERSION}/kubebuilder_linux_${ARCH}"

install_or_warn "kubectl" install_binary \
  "kubectl" \
  "${KUBECTL_VERSION}" \
  "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl"

install_or_warn "rust" install_rust

echo ""
echo "------------------------------------"
echo "Installing user-level bash completions..."
echo "------------------------------------"

if command -v kubebuilder >/dev/null 2>&1; then
  install_completion "kubebuilder" kubebuilder completion bash
fi

if command -v kubectl >/dev/null 2>&1; then
  install_completion "kubectl" kubectl completion bash
fi

if command -v docker >/dev/null 2>&1; then
  install_completion "docker" docker completion bash
fi

echo ""
echo "------------------------------------"
echo "Verifying installations..."
echo "------------------------------------"

verify_command "kubebuilder" kubebuilder version
verify_command "kubectl" kubectl version --client
verify_command "docker" docker --version
verify_command "go" go version

cat <<EOF

====================================
DevContainer ready.
====================================
Workspace: /workspace

Control-plane commands (no Kind required):
  cd /workspace/control-plane
  make test
  make lint
  make manifests generate

Project-managed Go tools are installed by the Makefile into:
  ${BIN_DIR}
EOF

if [ "${#FAILED_INSTALLS[@]}" -gt 0 ]; then
  printf '\nWARNING: Some optional tools were not installed due to network/proxy failures: %s\n' "${FAILED_INSTALLS[*]}" >&2
  echo "The devcontainer is usable, but rerun /workspace/.devcontainer/post-install.sh after fixing connectivity." >&2
fi

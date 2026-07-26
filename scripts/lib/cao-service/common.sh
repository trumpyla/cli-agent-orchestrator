#!/usr/bin/env bash
#
# Shared initialization, diagnostics, dependencies, and lifecycle locking.

# Write an operator-facing diagnostic to stderr.
cao_service::log() {
  printf 'cao-service: %s\n' "$*" >&2
}

# Write an error and terminate the controller.
cao_service::die() {
  cao_service::log "ERROR: $*"
  exit 1
}

# Print the public command contract.
cao_service::usage() {
  cat >&2 <<'EOF'
Usage: cao-service.sh {install|ensure|start|stop|restart|status|uninstall}
EOF
}

# Fail when a required executable is unavailable.
cao_service::require_command() {
  command -v "$1" >/dev/null 2>&1 ||
    cao_service::die "required command not found: $1"
}

# Derive immutable user paths and load the native platform adapter.
cao_service::init() {
  [[ -n "${HOME:-}" ]] || cao_service::die "HOME is not set"

  CAO_SERVICE_PLATFORM="$(uname -s)"
  CAO_SERVICE_UID="$(id -u)"
  CAO_RUNTIME_DIR="${HOME}/.local/libexec/cao-service"
  CAO_RUNTIME_LIB_DIR="${CAO_RUNTIME_DIR}/lib/cao-service"
  CAO_RUNTIME_RUNNER="${CAO_RUNTIME_DIR}/runner.sh"
  CAO_EXECUTABLE_FILE="${CAO_RUNTIME_DIR}/cao-server.path"
  CAO_STATE_DIR="${HOME}/.local/state/cao"
  CAO_CONFIG_DIR="${HOME}/.config/cao"
  CAO_ENV_FILE="${CAO_CONFIG_DIR}/service.env"
  CAO_SERVICE_URL="http://127.0.0.1:9889"
  CAO_SERVICE_READY_TIMEOUT="${CAO_SERVICE_READY_TIMEOUT:-30}"
  CAO_SERVICE_READY_INTERVAL="${CAO_SERVICE_READY_INTERVAL:-1}"
  CAO_SERVICE_LOCK="${CAO_STATE_DIR}/controller.lock"

  case "${CAO_SERVICE_PLATFORM}" in
    Darwin)
      # shellcheck source=scripts/lib/cao-service/macos.sh
      source "${CAO_SERVICE_LIB_DIR}/macos.sh"
      ;;
    Linux)
      # shellcheck source=scripts/lib/cao-service/linux.sh
      source "${CAO_SERVICE_LIB_DIR}/linux.sh"
      ;;
    *)
      cao_service::die \
        "unsupported operating system: ${CAO_SERVICE_PLATFORM}"
      ;;
  esac

  # These constants are consumed by sibling libraries sourced by the entrypoint.
  # shellcheck disable=SC2034
  readonly CAO_SERVICE_PLATFORM CAO_SERVICE_UID
  # shellcheck disable=SC2034
  readonly CAO_RUNTIME_DIR CAO_RUNTIME_LIB_DIR CAO_RUNTIME_RUNNER
  # shellcheck disable=SC2034
  readonly CAO_EXECUTABLE_FILE CAO_STATE_DIR CAO_CONFIG_DIR CAO_ENV_FILE
  # shellcheck disable=SC2034
  readonly CAO_SERVICE_URL CAO_SERVICE_READY_TIMEOUT
  # shellcheck disable=SC2034
  readonly CAO_SERVICE_READY_INTERVAL CAO_SERVICE_LOCK
}

# Validate common and platform-specific runtime dependencies.
cao_service::require_dependencies() {
  local command_name
  local commands=(
    awk basename cat chmod cmp cp curl dirname grep lsof mkdir mktemp
    mv rm rmdir sed sleep
  )
  for command_name in "${commands[@]}"; do
    cao_service::require_command "${command_name}"
  done
  cao_service::native::require_dependencies
}

# Serialize controller operations with an atomic directory lock.
cao_service::acquire_lock() {
  mkdir -p "${CAO_STATE_DIR}"
  chmod 700 "${CAO_STATE_DIR}"
  if ! mkdir "${CAO_SERVICE_LOCK}" 2>/dev/null; then
    cao_service::die "lifecycle operation is already running"
  fi
  trap 'cao_service::release_lock' EXIT HUP INT TERM
}

# Release only the lock directory owned by this invocation.
cao_service::release_lock() {
  if [[ -n "${CAO_SERVICE_LOCK:-}" && -d "${CAO_SERVICE_LOCK}" ]]; then
    rmdir "${CAO_SERVICE_LOCK}" 2>/dev/null || true
  fi
}

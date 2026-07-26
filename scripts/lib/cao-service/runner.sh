#!/usr/bin/env bash
#
# Validate private overrides and execute the installed cao-server.

set -euo pipefail
umask 077

# Write an error and terminate the installed runner.
cao_service_runner::die() {
  printf 'cao-service runner: ERROR: %s\n' "$*" >&2
  exit 1
}

# Fail when a runner dependency is unavailable.
cao_service_runner::require_command() {
  command -v "$1" >/dev/null 2>&1 ||
    cao_service_runner::die "required command not found: $1"
}

# Print a file's portable numeric permission mode.
cao_service_runner::file_mode() {
  case "$(uname -s)" in
    Darwin) stat -f '%Lp' "$1" ;;
    Linux) stat -c '%a' "$1" ;;
    *) cao_service_runner::die "unsupported operating system" ;;
  esac
}

# Print a file owner's numeric UID.
cao_service_runner::file_owner() {
  case "$(uname -s)" in
    Darwin) stat -f '%u' "$1" ;;
    Linux) stat -c '%u' "$1" ;;
    *) cao_service_runner::die "unsupported operating system" ;;
  esac
}

# Validate configuration, load overrides, and replace the runner process.
main() {
  local runner_dir state_dir env_file executable_file cao_server
  local command_name
  local commands=(chmod dirname id mkdir stat uname)
  for command_name in "${commands[@]}"; do
    cao_service_runner::require_command "${command_name}"
  done

  runner_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  state_dir="${HOME:?HOME is not set}/.local/state/cao"
  env_file="${HOME}/.config/cao/service.env"
  executable_file="${runner_dir}/cao-server.path"

  [[ -f "${executable_file}" ]] ||
    cao_service_runner::die "missing installed executable record"
  IFS= read -r cao_server <"${executable_file}"
  [[ -n "${cao_server}" && -x "${cao_server}" ]] ||
    cao_service_runner::die \
      "installed cao-server executable is unavailable"

  if [[ -e "${env_file}" ]]; then
    [[ -f "${env_file}" && ! -L "${env_file}" ]] ||
      cao_service_runner::die "service.env must be a regular file"
    [[ "$(cao_service_runner::file_owner "${env_file}")" == "$(id -u)" ]] ||
      cao_service_runner::die \
        "service.env must be owned by the current user"
    [[ "$(cao_service_runner::file_mode "${env_file}")" == "600" ]] ||
      cao_service_runner::die "service.env must have mode 600"
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
  fi

  mkdir -p "${state_dir}"
  chmod 700 "${state_dir}"
  cd "${state_dir}"
  exec "${cao_server}"
}

main "$@"

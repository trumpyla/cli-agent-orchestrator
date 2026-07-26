#!/usr/bin/env bash
#
# Public lifecycle command implementations and the controller entrypoint.

# Atomically install the platform definition and expose change state.
cao_service::write_definition() {
  local definition_temp
  cao_file_changed=0
  definition_temp="$(cao_service::make_temp_file "${CAO_NATIVE_DEFINITION}")"
  cao_service::native::render_definition >"${definition_temp}"
  cao_service::atomic_copy \
    "${definition_temp}" "${CAO_NATIVE_DEFINITION}" 600
  rm -f "${definition_temp}"
  cao_definition_changed="${cao_file_changed}"
}

# Install or update the runtime and ensure a healthy native service.
cao_service::install() {
  local was_loaded=0
  cao_runtime_changed=0
  cao_definition_changed=0
  cao_service::guard_listener
  cao_service::native::is_loaded && was_loaded=1
  cao_service::install_runtime
  cao_service::write_definition

  # Runtime refreshes keep the native label loaded; only definition changes
  # require an unload/bootstrap cycle.
  if ((was_loaded == 0)); then
    cao_service::native::enable_start
  elif ((cao_definition_changed)); then
    cao_service::native::reload
  elif ((cao_runtime_changed)); then
    cao_service::native::restart
  elif ! cao_service::native::is_active; then
    cao_service::native::start
  fi
  cao_service::wait_ready
  cao_service::log "installed and healthy"
}

# Start the installed service, or no-op when it is already healthy.
cao_service::start() {
  cao_service::native::is_installed ||
    cao_service::die "service is not installed; run install first"
  cao_service::guard_listener
  if cao_service::native::is_active &&
    cao_service::listener_is_owned &&
    cao_service::health_ok; then
    cao_service::log "already healthy"
    return
  fi
  cao_service::native::start
  cao_service::wait_ready
}

# Install when absent, otherwise ensure the service is healthy.
cao_service::ensure() {
  if ! cao_service::native::is_installed ||
    [[ ! -x "${CAO_RUNTIME_DIR}/cao-service.sh" ]]; then
    cao_service::install
    return
  fi
  cao_service::start
}

# Intentionally stop only the installed native service.
cao_service::stop() {
  cao_service::native::is_installed || return 0
  cao_service::native::stop
  cao_service::log "stopped"
}

# Restart only the installed native service and await readiness.
cao_service::restart() {
  cao_service::native::is_installed ||
    cao_service::die "service is not installed; run install first"
  cao_service::guard_listener
  cao_service::native::restart
  cao_service::wait_ready
}

# Remove controller-owned files while preserving user configuration and logs.
cao_service::uninstall() {
  cao_service::native::remove_definition
  cao_service::remove_runtime
  cao_service::log "uninstalled; preserved ${CAO_ENV_FILE} and state logs"
}

# Parse and execute one public lifecycle command.
main() {
  local command_name="${1:-}"
  case "${command_name}" in
    install | ensure | start | stop | restart | status | uninstall) ;;
    *)
      cao_service::usage
      return 2
      ;;
  esac

  cao_service::init
  cao_service::require_dependencies
  cao_service::acquire_lock

  case "${command_name}" in
    install) cao_service::install ;;
    ensure) cao_service::ensure ;;
    start) cao_service::start ;;
    stop) cao_service::stop ;;
    restart) cao_service::restart ;;
    status) cao_service::print_status ;;
    uninstall) cao_service::uninstall ;;
  esac
}

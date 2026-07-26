#!/usr/bin/env bash
#
# Listener ownership, HTTP readiness, and status reporting.

# Print PIDs listening on the fixed CAO API port.
cao_service::listener_pids() {
  lsof -nP -iTCP:9889 -sTCP:LISTEN -t 2>/dev/null || true
}

# Return success only when the native manager PID owns the listener.
cao_service::listener_is_owned() {
  local manager_pid listener_pid
  cao_service::native::is_active || return 1
  manager_pid="$(cao_service::native::pid)"
  [[ -n "${manager_pid}" && "${manager_pid}" != "0" ]] || return 1
  while IFS= read -r listener_pid; do
    [[ "${listener_pid}" == "${manager_pid}" ]] && return 0
  done < <(cao_service::listener_pids)
  return 1
}

# Refuse lifecycle mutation when an unmanaged process owns port 9889.
cao_service::guard_listener() {
  local listener_pids
  listener_pids="$(cao_service::listener_pids)"
  [[ -n "${listener_pids}" ]] || return 0
  cao_service::listener_is_owned && return 0
  cao_service::die \
    "port 9889 has an unmanaged listener; refusing to alter any process"
}

# Return success when CAO's health endpoint accepts the request.
cao_service::health_ok() {
  curl --fail --silent --show-error --max-time 2 \
    "${CAO_SERVICE_URL}/health" >/dev/null 2>&1
}

# Wait for owned-listener and HTTP readiness within the configured bound.
cao_service::wait_ready() {
  local started_at="${SECONDS}"
  while true; do
    if cao_service::listener_is_owned && cao_service::health_ok; then
      return
    fi
    if ((SECONDS - started_at >= CAO_SERVICE_READY_TIMEOUT)); then
      break
    fi
    sleep "${CAO_SERVICE_READY_INTERVAL}"
  done
  cao_service::log "ERROR: CAO did not become healthy at ${CAO_SERVICE_URL}"
  cao_service::native::status_hint
  return 1
}

# Print a stable status summary and return success only when fully healthy.
cao_service::print_status() {
  local manager_state="inactive"
  local pid="none"
  local listener_state="absent"
  local health_state="unhealthy"
  local installed_state="no"

  cao_service::native::is_installed && installed_state="yes"
  if cao_service::native::is_active; then
    manager_state="active"
    pid="$(cao_service::native::pid)"
  fi
  if [[ -n "$(cao_service::listener_pids)" ]]; then
    listener_state="unmanaged"
    cao_service::listener_is_owned && listener_state="owned"
  fi
  cao_service::health_ok && health_state="healthy"

  printf 'platform: %s\n' "${CAO_SERVICE_PLATFORM}"
  printf 'installed: %s\n' "${installed_state}"
  printf 'manager: %s\n' "${manager_state}"
  printf 'pid: %s\n' "${pid}"
  printf 'listener: %s\n' "${listener_state}"
  printf 'health: %s\n' "${health_state}"

  [[ "${installed_state}" == "yes" &&
    "${manager_state}" == "active" &&
    "${listener_state}" == "owned" &&
    "${health_state}" == "healthy" ]]
}

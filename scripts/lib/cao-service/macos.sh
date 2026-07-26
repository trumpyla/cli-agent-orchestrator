#!/usr/bin/env bash
#
# launchd adapter for the per-user CAO service.

CAO_NATIVE_LABEL="com.artagon.cao-server"
CAO_NATIVE_DEFINITION="${HOME}/Library/LaunchAgents/${CAO_NATIVE_LABEL}.plist"
CAO_NATIVE_TARGET="gui/${CAO_SERVICE_UID}/${CAO_NATIVE_LABEL}"
readonly CAO_NATIVE_LABEL CAO_NATIVE_DEFINITION CAO_NATIVE_TARGET

# Validate the launchd command dependency.
cao_service::native::require_dependencies() {
  cao_service::require_command launchctl
}

# Return success when the LaunchAgent definition exists.
cao_service::native::is_installed() {
  [[ -f "${CAO_NATIVE_DEFINITION}" ]]
}

# Return success when launchd knows the user service label.
cao_service::native::is_loaded() {
  launchctl print "${CAO_NATIVE_TARGET}" >/dev/null 2>&1
}

# Return success when launchd reports a running process.
cao_service::native::is_active() {
  local service_info
  service_info="$(launchctl print "${CAO_NATIVE_TARGET}" 2>/dev/null)" ||
    return 1
  [[ "${service_info}" == *"state = running"* ]]
}

# Print the PID owned by launchd.
cao_service::native::pid() {
  local service_info
  service_info="$(launchctl print "${CAO_NATIVE_TARGET}" 2>/dev/null)" ||
    return 1
  awk '$1 == "pid" { print $3; exit }' <<<"${service_info}"
}

# Render the LaunchAgent definition to stdout.
cao_service::native::render_definition() {
  local runner log_dir
  runner="$(printf '%s' "${CAO_RUNTIME_RUNNER}" |
    sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')"
  log_dir="$(printf '%s' "${CAO_STATE_DIR}" |
    sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')"
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${CAO_NATIVE_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>${runner}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${log_dir}/stdout.log</string>
  <key>StandardErrorPath</key><string>${log_dir}/stderr.log</string>
</dict>
</plist>
EOF
}

# Bootstrap launchd with a bounded retry for transient teardown races.
cao_service::native::bootstrap() {
  local attempt=1
  while ((attempt <= CAO_SERVICE_NATIVE_RETRY_ATTEMPTS)); do
    if launchctl bootstrap \
      "gui/${CAO_SERVICE_UID}" "${CAO_NATIVE_DEFINITION}"; then
      return 0
    fi
    if cao_service::native::is_loaded; then
      return 0
    fi
    if ((attempt == CAO_SERVICE_NATIVE_RETRY_ATTEMPTS)); then
      cao_service::log \
        "ERROR: launchd bootstrap failed after ${attempt} attempts"
      return 1
    fi
    sleep "${CAO_SERVICE_NATIVE_RETRY_INTERVAL}"
    attempt=$((attempt + 1))
  done
}

# Load and start the LaunchAgent.
cao_service::native::enable_start() {
  cao_service::native::bootstrap
}

# Start the loaded service or bootstrap an unloaded definition.
cao_service::native::start() {
  if cao_service::native::is_loaded; then
    launchctl kickstart -k "${CAO_NATIVE_TARGET}"
  else
    cao_service::native::enable_start
  fi
}

# Intentionally unload the LaunchAgent.
cao_service::native::stop() {
  cao_service::native::is_loaded &&
    launchctl bootout "${CAO_NATIVE_TARGET}"
  return 0
}

# Restart only the owned LaunchAgent.
cao_service::native::restart() {
  cao_service::native::start
}

# Reload a changed LaunchAgent definition.
cao_service::native::reload() {
  cao_service::native::stop
  cao_service::native::enable_start
}

# Unload the service and remove only its definition.
cao_service::native::remove_definition() {
  cao_service::native::stop
  rm -f "${CAO_NATIVE_DEFINITION}"
}

# Print launchd-specific troubleshooting commands.
cao_service::native::status_hint() {
  cao_service::log "inspect: launchctl print ${CAO_NATIVE_TARGET}"
  cao_service::log "logs: ${CAO_STATE_DIR}/stderr.log"
}

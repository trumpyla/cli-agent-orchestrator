#!/usr/bin/env bash
#
# systemd user-service adapter for the per-user CAO service.

CAO_NATIVE_UNIT="cao-server.service"
CAO_NATIVE_DEFINITION="${HOME}/.config/systemd/user/${CAO_NATIVE_UNIT}"
readonly CAO_NATIVE_UNIT CAO_NATIVE_DEFINITION

# Validate the systemd command dependency.
cao_service::native::require_dependencies() {
  cao_service::require_command systemctl
}

# Return success when the user unit definition exists.
cao_service::native::is_installed() {
  [[ -f "${CAO_NATIVE_DEFINITION}" ]]
}

# Return success when systemd reports the user unit active.
cao_service::native::is_loaded() {
  cao_service::native::is_active
}

# Return success when the user unit is active.
cao_service::native::is_active() {
  systemctl --user is-active --quiet "${CAO_NATIVE_UNIT}"
}

# Print systemd's MainPID for the user unit.
cao_service::native::pid() {
  systemctl --user show \
    --property MainPID --value "${CAO_NATIVE_UNIT}" 2>/dev/null
}

# Render the systemd user unit to stdout.
cao_service::native::render_definition() {
  local runner
  runner="${CAO_RUNTIME_RUNNER//\\/\\\\}"
  runner="${runner//\"/\\\"}"
  runner="${runner//%/%%}"
  cat <<EOF
[Unit]
Description=CLI Agent Orchestrator server
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
ExecStart="${runner}"
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
}

# Enable login startup and start the user unit.
cao_service::native::enable_start() {
  systemctl --user daemon-reload
  systemctl --user enable --now "${CAO_NATIVE_UNIT}"
}

# Start the installed user unit.
cao_service::native::start() {
  systemctl --user start "${CAO_NATIVE_UNIT}"
}

# Intentionally stop the user unit without disabling login startup.
cao_service::native::stop() {
  cao_service::native::is_active &&
    systemctl --user stop "${CAO_NATIVE_UNIT}"
  return 0
}

# Restart only the owned user unit.
cao_service::native::restart() {
  systemctl --user restart "${CAO_NATIVE_UNIT}"
}

# Reload a changed unit and preserve its running intent.
cao_service::native::reload() {
  systemctl --user daemon-reload
  if cao_service::native::is_active; then
    cao_service::native::restart
  else
    systemctl --user enable --now "${CAO_NATIVE_UNIT}"
  fi
}

# Disable the user unit and remove only its definition.
cao_service::native::remove_definition() {
  if cao_service::native::is_installed; then
    systemctl --user disable --now "${CAO_NATIVE_UNIT}"
    rm -f "${CAO_NATIVE_DEFINITION}"
    systemctl --user daemon-reload
  fi
}

# Print systemd-specific troubleshooting commands.
cao_service::native::status_hint() {
  cao_service::log "inspect: systemctl --user status ${CAO_NATIVE_UNIT}"
  cao_service::log "logs: journalctl --user -u ${CAO_NATIVE_UNIT}"
}

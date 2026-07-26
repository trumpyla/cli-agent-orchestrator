#!/usr/bin/env bats

load helpers/cao_service_test_helper

@test "unknown command fails with usage" {
    setup_cao_service_test Darwin

    run_service unknown

    assert_failure
    assert_output_contains "Usage:"
}

@test "Darwin install writes crash-only LaunchAgent and stable runtime" {
    setup_cao_service_test Darwin

    run_service install

    assert_success
    plist="${HOME}/Library/LaunchAgents/com.artagon.cao-server.plist"
    assert_file_contains "${plist}" "<key>SuccessfulExit</key>"
    assert_file_contains "${plist}" "<key>ThrottleInterval</key>"
    assert_file_exists "${HOME}/.local/libexec/cao-service/runner.sh"
    assert_file_exists "${HOME}/.local/libexec/cao-service/cao-service.sh"
}

@test "Linux install writes enabled restart-on-failure user unit" {
    setup_cao_service_test Linux

    run_service install

    assert_success
    unit="${HOME}/.config/systemd/user/cao-server.service"
    assert_file_contains "${unit}" "Restart=on-failure"
    assert_file_contains "${unit}" "StartLimitBurst=3"
    [[ "$(manager_call_count " enable ")" -eq 1 ]]
}

@test "Darwin ensure is a healthy no-op with the same PID" {
    setup_cao_service_test Darwin
    run_service install
    assert_success
    first_pid="$(cat "${CAO_TEST_STATE}/pid")"

    run_service ensure

    assert_success
    [[ "$(cat "${CAO_TEST_STATE}/pid")" == "${first_pid}" ]]
  [[ "$(manager_call_count bootstrap)" -eq 1 ]]
}

@test "repeated Darwin install does not reload unchanged service" {
  setup_cao_service_test Darwin
  run_service install
  assert_success
  first_pid="$(cat "${CAO_TEST_STATE}/pid")"

  run_service install

  assert_success
  [[ "$(cat "${CAO_TEST_STATE}/pid")" == "${first_pid}" ]]
  [[ "$(manager_call_count bootstrap)" -eq 1 ]]
  [[ "$(manager_call_count bootout)" -eq 0 ]]
}

@test "copied controller runs independently from installed runtime" {
  setup_cao_service_test Linux
  run_service install
  assert_success

  run "${HOME}/.local/libexec/cao-service/cao-service.sh" status

  assert_success
  assert_output_contains "manager: active"
  assert_output_contains "health: healthy"
}

@test "Linux start is idempotent while healthy" {
    setup_cao_service_test Linux
    run_service install
    assert_success

    run_service start

    assert_success
    [[ "$(manager_call_count " start ")" -eq 0 ]]
}

@test "held lifecycle lock rejects a concurrent command" {
  setup_cao_service_test Linux
  mkdir -p "${HOME}/.local/state/cao/controller.lock"
  printf '%s\n' "$$" >"${HOME}/.local/state/cao/controller.lock/owner_pid"

  run_service status

  assert_failure
  assert_output_contains "lifecycle operation is already running"
}

@test "stale lifecycle lock is recovered without broad process cleanup" {
  setup_cao_service_test Linux
  mkdir -p "${HOME}/.local/state/cao/controller.lock"
  printf '%s\n' "99999999" \
    >"${HOME}/.local/state/cao/controller.lock/owner_pid"

  run_service status

  assert_failure
  refute_output_contains "lifecycle operation is already running"
  refute_file_exists "${HOME}/.local/state/cao/controller.lock"
}

@test "symlinked state directory fails closed" {
  setup_cao_service_test Darwin
  mkdir -p "${TEST_ROOT}/redirected-state"
  mkdir -p "${HOME}/.local/state"
  ln -s "${TEST_ROOT}/redirected-state" "${HOME}/.local/state/cao"

  run_service status

  assert_failure
  assert_output_contains "state directory must not be a symlink"
}

@test "unmanaged listener blocks install without kill or adoption" {
    setup_cao_service_test Darwin
    touch "${CAO_TEST_STATE}/unmanaged-listener"

    run_service install

    assert_failure
    assert_output_contains "unmanaged listener"
    refute_file_exists "${CAO_TEST_STATE}/killed"
    refute_file_exists "${CAO_TEST_STATE}/loaded"
}

@test "readiness timeout returns failure with diagnostic guidance" {
    setup_cao_service_test Linux
    export CAO_SERVICE_READY_TIMEOUT=0
    touch "${CAO_TEST_STATE}/suppress-health"

    run_service install

    assert_failure
    assert_output_contains "did not become healthy"
    assert_output_contains "systemctl --user status"
}

@test "runner rejects service environment file that is not mode 600" {
    setup_cao_service_test Linux
    run_service install
    assert_success
    mkdir -p "${HOME}/.config/cao"
    printf '%s\n' "CAO_TEST_VALUE=secret-value" >"${HOME}/.config/cao/service.env"
    chmod 644 "${HOME}/.config/cao/service.env"

    run "${HOME}/.local/libexec/cao-service/runner.sh"

    assert_failure
    assert_output_contains "mode 600"
    refute_output_contains "secret-value"
}

@test "runner loads private environment and uses stable state directory" {
    setup_cao_service_test Darwin
    run_service install
    assert_success
    mkdir -p "${HOME}/.config/cao"
    printf '%s\n' "CAO_TEST_VALUE=loaded" >"${HOME}/.config/cao/service.env"
    chmod 600 "${HOME}/.config/cao/service.env"

    run "${HOME}/.local/libexec/cao-service/runner.sh"

    assert_success
    assert_file_contains "${CAO_TEST_STATE}/server-env-value" "loaded"
    assert_file_contains "${CAO_TEST_STATE}/server-working-directory" \
        "${HOME}/.local/state/cao"
}

@test "status reports platform manager PID listener and health" {
    setup_cao_service_test Darwin
    run_service install
    assert_success

    run_service status

    assert_success
    assert_output_contains "platform: Darwin"
    assert_output_contains "manager: active"
    assert_output_contains "pid: 4242"
    assert_output_contains "listener: owned"
    assert_output_contains "health: healthy"
}

@test "Darwin stop unloads only the owned label" {
    setup_cao_service_test Darwin
    run_service install
    assert_success

    run_service stop

    assert_success
    assert_file_contains "${CAO_TEST_STATE}/calls" \
        "bootout gui/$(id -u)/com.artagon.cao-server"
    refute_file_exists "${CAO_TEST_STATE}/active"
}

@test "Linux stop is intentional and leaves login enablement" {
    setup_cao_service_test Linux
    run_service install
    assert_success

    run_service stop

    assert_success
    [[ "$(manager_call_count " stop ")" -eq 1 ]]
    assert_file_exists "${CAO_TEST_STATE}/enabled"
    refute_file_exists "${CAO_TEST_STATE}/active"
}

@test "Linux restart targets only installed user unit" {
    setup_cao_service_test Linux
    run_service install
    assert_success

    run_service restart

    assert_success
    assert_file_contains "${CAO_TEST_STATE}/calls" \
        "systemctl --user restart cao-server.service"
}

@test "uninstall removes owned runtime and definition but preserves user config" {
    setup_cao_service_test Linux
    run_service install
    assert_success
    mkdir -p "${HOME}/.config/cao"
    printf '%s\n' "CAO_TEST_VALUE=keep" >"${HOME}/.config/cao/service.env"
    chmod 600 "${HOME}/.config/cao/service.env"

    run_service uninstall

    assert_success
    refute_file_exists "${HOME}/.config/systemd/user/cao-server.service"
    refute_file_exists "${HOME}/.local/libexec/cao-service/cao-service.sh"
    assert_file_exists "${HOME}/.config/cao/service.env"
}

@test "unsupported operating system fails closed" {
    setup_cao_service_test FreeBSD

    run_service status

    assert_failure
    assert_output_contains "unsupported operating system"
}

@test "committed service scripts contain no machine-specific home" {
    setup_cao_service_test Darwin

    run grep -R -E "/Users/|/home/[^$]" \
        "${REPO_ROOT}/scripts/cao-service.sh" \
        "${REPO_ROOT}/scripts/lib/cao-service"

  assert_failure
}

@test "temporary files use target-derived names" {
  setup_cao_service_test Darwin

  run grep -R -E "cao-(executable|definition).*XXXXXX" \
    "${REPO_ROOT}/scripts/cao-service.sh" \
    "${REPO_ROOT}/scripts/lib/cao-service"

  assert_failure
}

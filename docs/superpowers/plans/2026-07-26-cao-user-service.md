# CAO Per-User Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install and manage exactly one crash-resilient `cao-server` per user
on macOS or Linux without depending on the repository checkout.

**Architecture:** A portable Bash controller copies a small runtime into
`~/.local/libexec/cao-service`, serializes lifecycle operations with an atomic
directory lock, and delegates process ownership to LaunchAgents or
`systemd --user`. A shared runner validates and loads the optional private
environment file, then executes the exact `cao-server` path captured at
installation.

**Tech Stack:** Bash 3.2-compatible shell, launchd LaunchAgents,
`systemd --user`, Bats, ShellCheck, shfmt, GitHub Actions.

## Global Constraints

- The service is user-scoped and never requires root privileges.
- Runtime files live under `~/.local/libexec/cao-service`; state and logs live
  under `~/.local/state/cao`.
- Optional overrides come only from mode-600 `~/.config/cao/service.env`.
- The managed API is fixed at `127.0.0.1:9889`.
- The controller never kills, adopts, or starts beside an unmanaged listener.
- Native managers own crash restart and login startup.
- Committed files contain no machine-specific absolute paths.
- Shell code uses strict mode, quoted expansions, stderr diagnostics, atomic
  writes, and explicit dependency validation.

## File Map

- `scripts/cao-service.sh`: public command dispatcher and lifecycle sequencing.
- `scripts/lib/cao-service/common.sh`: paths, logging, atomic writes, locking,
  listener ownership, readiness, and installed-runtime management.
- `scripts/lib/cao-service/macos.sh`: LaunchAgent rendering and `launchctl`
  operations.
- `scripts/lib/cao-service/linux.sh`: systemd user-unit rendering and
  `systemctl --user` operations.
- `scripts/lib/cao-service/runner.sh`: private environment validation and the
  final `exec` of the captured server executable.
- `test/scripts/helpers/cao_service_test_helper.bash`: isolated HOME/PATH and
  fake native-manager state helpers.
- `test/scripts/fixtures/cao-service/bin/*`: deterministic `uname`, `id`,
  `launchctl`, `systemctl`, `lsof`, `curl`, and `cao-server` doubles.
- `test/scripts/cao_service.bats`: cross-platform controller contract tests.
- `.github/workflows/ci.yml`: Ubuntu/macOS Bats and shell-quality job.
- `docs/configuration.md`: installation, lifecycle, environment, diagnostics,
  and uninstall guidance.

---

### Task 1: Bats Harness and Failing Public-Contract Tests

**Files:**
- Create: `test/scripts/helpers/cao_service_test_helper.bash`
- Create: `test/scripts/fixtures/cao-service/bin/uname`
- Create: `test/scripts/fixtures/cao-service/bin/id`
- Create: `test/scripts/fixtures/cao-service/bin/launchctl`
- Create: `test/scripts/fixtures/cao-service/bin/systemctl`
- Create: `test/scripts/fixtures/cao-service/bin/lsof`
- Create: `test/scripts/fixtures/cao-service/bin/curl`
- Create: `test/scripts/fixtures/cao-service/bin/cao-server`
- Create: `test/scripts/cao_service.bats`

**Interfaces:**
- Produces: `setup_cao_service_test PLATFORM`,
  `run_service COMMAND`, `manager_call_count OPERATION`, and state files under
  `$CAO_TEST_STATE`.
- Consumes: `scripts/cao-service.sh COMMAND`.

- [ ] **Step 1: Build the isolated test environment and command doubles**

The helper creates a temporary HOME, prepends the fixture directory to PATH,
sets `CAO_SERVICE_READY_TIMEOUT=1`, and records native-manager calls in
`$CAO_TEST_STATE/calls`. Manager doubles implement bootstrap/bootout/kickstart
and daemon-reload/enable/start/stop/restart/is-active/show. `lsof` reports either
the managed PID or PID 999 for an `unmanaged-listener` marker; `curl` succeeds
only when the `healthy` marker exists.

```bash
setup_cao_service_test() {
    export FAKE_PLATFORM="$1"
    export TEST_ROOT="$BATS_TEST_TMPDIR/root"
    export HOME="$TEST_ROOT/home"
    export CAO_TEST_STATE="$TEST_ROOT/state"
    export CAO_SERVICE_READY_TIMEOUT=1
    export PATH="$BATS_TEST_DIRNAME/fixtures/cao-service/bin:$ORIGINAL_PATH"
    mkdir -p "$HOME" "$CAO_TEST_STATE"
}

run_service() {
    run "$BATS_TEST_DIRNAME/../../scripts/cao-service.sh" "$@"
}
```

- [ ] **Step 2: Write the failing public-contract tests**

Create named tests for help/unknown commands, Darwin install, Linux install,
stable runtime copying, repeated ensure/start idempotency, lock contention,
unmanaged-listener refusal, readiness timeout, private environment loading,
status, stop, restart, and uninstall.

```bash
@test "Darwin install writes a crash-only LaunchAgent and becomes healthy" {
    setup_cao_service_test Darwin
    run_service install
    assert_success
    assert_file_contains \
        "$HOME/Library/LaunchAgents/com.artagon.cao-server.plist" \
        "<key>SuccessfulExit</key>"
    assert_file_contains \
        "$HOME/Library/LaunchAgents/com.artagon.cao-server.plist" \
        "<key>ThrottleInterval</key>"
    assert_file_exists "$HOME/.local/libexec/cao-service/runner.sh"
}

@test "Linux install writes an enabled restart-on-failure user unit" {
    setup_cao_service_test Linux
    run_service install
    assert_success
    assert_file_contains \
        "$HOME/.config/systemd/user/cao-server.service" \
        "Restart=on-failure"
    [ "$(manager_call_count enable)" -eq 1 ]
}

@test "an unmanaged listener blocks start without kill or adoption" {
    setup_cao_service_test Darwin
    touch "$CAO_TEST_STATE/unmanaged-listener"
    run_service install
    assert_failure
    assert_output --partial "unmanaged listener"
    [ ! -e "$CAO_TEST_STATE/killed" ]
}
```

- [ ] **Step 3: Run Bats and verify RED**

Run: `rtk bats test/scripts/cao_service.bats`

Expected: FAIL because `scripts/cao-service.sh` does not exist.

- [ ] **Step 4: Commit the failing tests**

```bash
git add test/scripts
git commit -m "test: define CAO user service contract"
```

### Task 2: Shared Runtime, Locking, and Runner

**Files:**
- Create: `scripts/lib/cao-service/common.sh`
- Create: `scripts/lib/cao-service/runner.sh`
- Modify: `test/scripts/cao_service.bats`

**Interfaces:**
- Produces: `cao_service_init`, `cao_service_acquire_lock`,
  `cao_service_install_runtime`, `cao_service_guard_listener`,
  `cao_service_wait_ready`, `cao_service_remove_runtime`.
- Consumes: platform functions `native_is_active`, `native_pid`, and the fixed
  endpoint `http://127.0.0.1:9889/health`.

- [ ] **Step 1: Add focused failing tests for shared invariants**

```bash
@test "a held lifecycle lock rejects a concurrent command" {
    setup_cao_service_test Linux
    mkdir -p "$HOME/.local/state/cao/controller.lock"
    run_service status
    assert_failure
    assert_output --partial "lifecycle operation is already running"
}

@test "runner rejects a service environment file that is not mode 600" {
    setup_cao_service_test Linux
    mkdir -p "$HOME/.config/cao"
    printf '%s\n' 'CAO_TEST_VALUE=secret' >"$HOME/.config/cao/service.env"
    chmod 644 "$HOME/.config/cao/service.env"
    run_service install
    assert_failure
    refute_output --partial "secret"
}
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:
`rtk bats --filter 'held lifecycle lock|runner rejects' test/scripts/cao_service.bats`

Expected: FAIL because locking and runner validation are not implemented.

- [ ] **Step 3: Implement shared primitives and the runner**

Use `mkdir "$CAO_STATE_DIR/controller.lock"` as the portable atomic lock and a
trap that removes only that exact directory. Write copied runtime files through
`mktemp`, `chmod`, and `mv`; compare with `cmp` to avoid unnecessary reloads.
Store the resolved executable as one line in
`$CAO_RUNTIME_DIR/cao-server.path`. Require `curl`, `lsof`, `mktemp`, and the
native manager command. The runner checks owner UID and portable mode output
before sourcing `service.env` with `set -a`, changes to the state directory,
and finishes with:

```bash
exec "$cao_server"
```

Listener ownership succeeds only when `native_pid` is among
`lsof -nP -iTCP:9889 -sTCP:LISTEN -t` results. Readiness requires both that
owned listener and `curl --fail --silent --max-time 2` to `/health`.

- [ ] **Step 4: Run the shared-runtime tests and verify GREEN**

Run:
`rtk bats --filter 'held lifecycle lock|runner rejects|unmanaged listener' test/scripts/cao_service.bats`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/cao-service test/scripts
git commit -m "feat: add safe CAO service runtime"
```

### Task 3: macOS LaunchAgent Adapter and Lifecycle

**Files:**
- Create: `scripts/lib/cao-service/macos.sh`
- Create: `scripts/cao-service.sh`
- Modify: `test/scripts/cao_service.bats`

**Interfaces:**
- Produces: `native_definition_path`, `native_render_definition`,
  `native_is_installed`, `native_is_loaded`, `native_is_active`, `native_pid`,
  `native_enable_start`, `native_start`, `native_stop`, `native_restart`,
  `native_reload`, `native_uninstall`.
- Consumes: common runtime functions and `launchctl gui/$UID`.

- [ ] **Step 1: Add failing Darwin lifecycle tests**

```bash
@test "Darwin ensure is a healthy no-op and keeps the same managed PID" {
    setup_cao_service_test Darwin
    run_service install
    assert_success
    first_pid="$(cat "$CAO_TEST_STATE/pid")"
    run_service ensure
    assert_success
    [ "$(cat "$CAO_TEST_STATE/pid")" = "$first_pid" ]
    [ "$(manager_call_count bootstrap)" -eq 1 ]
}

@test "Darwin stop unloads only the owned label" {
    setup_cao_service_test Darwin
    run_service install
    run_service stop
    assert_success
    assert_file_contains "$CAO_TEST_STATE/calls" \
        "bootout gui/501/com.artagon.cao-server"
}
```

- [ ] **Step 2: Run Darwin tests and verify RED**

Run: `FAKE_PLATFORM=Darwin rtk bats test/scripts/cao_service.bats`

Expected: Darwin lifecycle assertions fail.

- [ ] **Step 3: Implement the LaunchAgent and dispatcher**

Render a private plist at
`~/Library/LaunchAgents/com.artagon.cao-server.plist` with:

```xml
<key>RunAtLoad</key><true/>
<key>KeepAlive</key>
<dict><key>SuccessfulExit</key><false/></dict>
<key>ThrottleInterval</key><integer>10</integer>
```

Use `launchctl bootstrap`, `bootout`, `kickstart -k`, and
`print gui/$UID/com.artagon.cao-server`; never use process-name matching.
Implement the seven public commands with one lock per invocation, fail-closed
listener checks before mutating starts, and readiness checks after starts.

- [ ] **Step 4: Run all Darwin tests and verify GREEN**

Run: `FAKE_PLATFORM=Darwin rtk bats test/scripts/cao_service.bats`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/cao-service.sh scripts/lib/cao-service/macos.sh test/scripts
git commit -m "feat: manage CAO with a macOS LaunchAgent"
```

### Task 4: Linux systemd User Adapter and Lifecycle

**Files:**
- Create: `scripts/lib/cao-service/linux.sh`
- Modify: `scripts/cao-service.sh`
- Modify: `test/scripts/cao_service.bats`

**Interfaces:**
- Produces the same `native_*` interface as the macOS adapter.
- Consumes: `systemctl --user`, the installed runner path, and common runtime
  functions.

- [ ] **Step 1: Add failing Linux lifecycle tests**

```bash
@test "Linux crash policy restarts failures but stop remains intentional" {
    setup_cao_service_test Linux
    run_service install
    assert_success
    assert_file_contains \
        "$HOME/.config/systemd/user/cao-server.service" \
        "Restart=on-failure"
    run_service stop
    assert_success
    [ "$(manager_call_count stop)" -eq 1 ]
}

@test "Linux restart targets only the installed user unit" {
    setup_cao_service_test Linux
    run_service install
    run_service restart
    assert_success
    assert_file_contains "$CAO_TEST_STATE/calls" \
        "--user restart cao-server.service"
}
```

- [ ] **Step 2: Run Linux tests and verify RED**

Run: `FAKE_PLATFORM=Linux rtk bats test/scripts/cao_service.bats`

Expected: Linux lifecycle assertions fail.

- [ ] **Step 3: Implement the systemd user adapter**

Render `~/.config/systemd/user/cao-server.service` with:

```ini
[Unit]
Description=CLI Agent Orchestrator server
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
ExecStart="/home-path/.local/libexec/cao-service/runner.sh"
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Use only `systemctl --user daemon-reload`, `enable --now`, `start`, `stop`,
`restart`, `disable --now`, `is-active`, and
`show --property MainPID --value`.

- [ ] **Step 4: Run all Linux tests and verify GREEN**

Run: `FAKE_PLATFORM=Linux rtk bats test/scripts/cao_service.bats`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/cao-service/linux.sh scripts/cao-service.sh test/scripts
git commit -m "feat: manage CAO with a systemd user service"
```

### Task 5: Documentation and CI Gates

**Files:**
- Modify: `docs/configuration.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `test/scripts/cao_service.bats`

**Interfaces:**
- Produces: user installation and recovery documentation plus a
  `cao-user-service` CI job.
- Consumes: the public command contract from Task 3 and Task 4.

- [ ] **Step 1: Add portability and documentation assertions**

```bash
@test "committed service files contain no installer machine home" {
    run grep -R "$HOME" \
        "$BATS_TEST_DIRNAME/../../scripts/cao-service.sh" \
        "$BATS_TEST_DIRNAME/../../scripts/lib/cao-service"
    assert_failure
}
```

- [ ] **Step 2: Run the portability test**

Run:
`rtk bats --filter 'committed service files contain no installer machine home' test/scripts/cao_service.bats`

Expected: PASS because committed scripts derive paths from `HOME`.

- [ ] **Step 3: Document the exact operator workflow**

Document:

```text
scripts/cao-service.sh install
scripts/cao-service.sh ensure
scripts/cao-service.sh status
scripts/cao-service.sh restart
scripts/cao-service.sh stop
scripts/cao-service.sh uninstall
```

Explain `~/.config/cao/service.env`, its mandatory `chmod 600`, stable runtime
location, native log commands, unmanaged-port refusal, and that repository
profiles continue resolving from each session working directory. State
explicitly that the service hosts the HTTP CAO Ops endpoint at `/mcp/ops`,
while the identity-bearing `cao-mcp-server` remains a command-launched stdio
entry; stale user-generated `uvx --from ...@COMMIT` profile pins must be
regenerated separately and are never rewritten by the service controller.

- [ ] **Step 4: Add the cross-platform CI job**

Add an Ubuntu/macOS matrix that installs `bats-core`, ShellCheck, shfmt, and
`lsof`, then runs:

```bash
bats test/scripts/cao_service.bats
shellcheck scripts/cao-service.sh scripts/lib/cao-service/*.sh \
  test/scripts/fixtures/cao-service/bin/*
shfmt -d -i 4 -ci scripts/cao-service.sh scripts/lib/cao-service/*.sh \
  test/scripts/fixtures/cao-service/bin/*
```

- [ ] **Step 5: Run local Bats and formatting gates**

Run:

```bash
rtk bats test/scripts/cao_service.bats
shellcheck scripts/cao-service.sh scripts/lib/cao-service/*.sh \
  test/scripts/fixtures/cao-service/bin/*
shfmt -d -i 4 -ci scripts/cao-service.sh scripts/lib/cao-service/*.sh \
  test/scripts/fixtures/cao-service/bin/*
uv run python scripts/validate_markdown_links.py
rtk git diff --check
```

Expected: every command exits 0.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml docs/configuration.md scripts test/scripts
git commit -m "ci: verify CAO user service lifecycle"
```

### Task 6: Native macOS Smoke Test and Final Verification

**Files:**
- No committed file changes expected.

**Interfaces:**
- Consumes: installed controller, LaunchAgent, `/health`, and `/mcp/ops`.
- Produces: retained command output proving singleton and endpoint behavior.

- [ ] **Step 1: Install and capture the managed PID**

Run:

```bash
scripts/cao-service.sh install
scripts/cao-service.sh status
launchctl print "gui/$(id -u)/com.artagon.cao-server"
```

Expected: install and status exit 0; one active PID is reported.

- [ ] **Step 2: Verify idempotent ensure**

Run:

```bash
before="$(launchctl print "gui/$(id -u)/com.artagon.cao-server")"
scripts/cao-service.sh ensure
after="$(launchctl print "gui/$(id -u)/com.artagon.cao-server")"
```

Expected: the reported PID is unchanged and no second listener exists.

- [ ] **Step 3: Verify HTTP and embedded MCP**

Run:

```bash
curl --fail --silent http://127.0.0.1:9889/health
```

Then run the repository MCP protocol probe against
`http://127.0.0.1:9889/mcp/ops` and verify initialize, tools/list, and one
read-only tool call retain the negotiated MCP session ID.

- [ ] **Step 4: Run the complete focused verification set**

Run:

```bash
rtk bats test/scripts/cao_service.bats
uv run pytest test/ops_mcp_server/test_embedded_http.py -q
shellcheck scripts/cao-service.sh scripts/lib/cao-service/*.sh \
  test/scripts/fixtures/cao-service/bin/*
shfmt -d -i 4 -ci scripts/cao-service.sh scripts/lib/cao-service/*.sh \
  test/scripts/fixtures/cao-service/bin/*
uv run python scripts/validate_markdown_links.py
rtk git diff --check
```

Expected: every command exits 0, the LaunchAgent remains healthy, and exactly
one process owns port 9889.

# CAO Per-User Service Design

## Objective

Provide one repository-owned controller that installs and manages `cao-server`
as a crash-resilient per-user service on macOS and Linux. Repeated or concurrent
lifecycle commands must never create multiple managed CAO instances.

## User Contract

`scripts/cao-service.sh` provides:

- `install`: install the controller runtime and native user service, enable
  login startup, and start CAO.
- `ensure`: install when absent, start when stopped, and no-op when healthy.
- `start`: start the installed service.
- `stop`: intentionally stop the installed service without automatic restart.
- `restart`: restart only the installed managed service.
- `status`: report platform, installation, manager state, PID, listener state,
  and `/health`; return a nonzero status unless CAO is healthy.
- `uninstall`: stop and remove only files owned by this controller.

The service is user-scoped and requires no root privileges.

## Architecture

The portable controller detects Darwin or Linux and dispatches to small
platform adapters under `scripts/lib/cao-service/`. Installation copies the
controller runtime into `~/.local/libexec/cao-service/`, so the service does not
depend on a repository checkout. Generated files may contain resolved local
paths; committed files must not.

macOS uses the LaunchAgent label `com.artagon.cao-server`, installed at
`~/Library/LaunchAgents/com.artagon.cao-server.plist`. It uses `RunAtLoad`,
restart-on-failure `KeepAlive`, and a throttle interval.

Linux uses the `systemd --user` unit `cao-server.service`, installed at
`~/.config/systemd/user/cao-server.service`. It uses `Restart=on-failure`,
bounded start-rate controls, and `systemctl --user enable --now`.

Both definitions invoke the same installed runner. The runner changes to
`~/.local/state/cao`, loads optional shell-style environment overrides from
mode-600 `~/.config/cao/service.env`, and `exec`s the exact `cao-server`
executable resolved during installation.

## Singleton and Ownership Rules

Native service managers provide the primary single-process guarantee. A
portable controller lock serializes lifecycle operations.

Before install, ensure, start, or restart, the controller checks port `9889`.
When the port is occupied but the native service manager does not own a running
CAO service, the command fails closed. It never kills, adopts, or starts beside
an unmanaged listener.

Repeated installation and start operations are idempotent. `stop` and
`uninstall` address only the native service label/unit and never use broad
`pkill`, process-name matching, or tmux cleanup.

After start or restart, readiness requires both a listener on
`127.0.0.1:9889` and a successful `GET /health` within a bounded timeout.
Failure returns nonzero and surfaces native service status/log guidance.

## Files and State

- Controller runtime: `~/.local/libexec/cao-service/`
- Optional environment: `~/.config/cao/service.env`
- State and macOS logs: `~/.local/state/cao/`
- macOS definition: `~/Library/LaunchAgents/com.artagon.cao-server.plist`
- Linux definition: `~/.config/systemd/user/cao-server.service`

Generated definitions and runtime files are written atomically with private or
non-world-writable modes. Environment values are never printed.

## Error Handling

Unsupported operating systems, missing dependencies, missing/non-executable
`cao-server`, unavailable user service managers, unsafe file ownership, an
unmanaged listener, readiness timeout, and native manager failures all fail
closed with actionable stderr messages.

Intentional stop is distinct from crash recovery: unloading/stopping the native
unit suppresses restart, while unexpected server failure is restarted with
throttling.

## Testing and Gates

Bats tests run with isolated fake homes and PATH stubs for `launchctl`,
`systemctl`, `curl`, listener inspection, and `cao-server`. They cover:

- macOS and Linux service generation;
- stable copied runtime independent of the checkout;
- repeated install/start/ensure idempotency;
- serialized concurrent lifecycle operations;
- unmanaged-listener refusal without process termination;
- restart-on-failure and intentional-stop behavior;
- optional environment loading without value disclosure;
- bounded readiness success and failure;
- safe status and uninstall semantics.

ShellCheck and shfmt validate every committed shell file. CI runs Bats on both
Ubuntu and macOS.

## Non-Goals

- System-wide/root service installation.
- Managing CAO tmux/Herdr sessions or MCP client registration.
- Killing or adopting an existing unmanaged CAO process.
- Running one service per repository or supporting custom CAO API ports.

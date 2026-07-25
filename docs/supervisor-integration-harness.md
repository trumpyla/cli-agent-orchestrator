# Supervisor integration harness

CAO has two complementary supervisor harnesses. They deliberately separate
deterministic orchestration coverage from authenticated provider compatibility.

## Deterministic CI harness

[`test/integration/test_supervisor_harness.py`](../test/integration/test_supervisor_harness.py)
starts a fresh source-backed `cao-server` with an isolated home, database, port,
and `mock_cli` process. It exercises the real REST and in-session MCP
boundaries:

1. create a supervisor and wait for readiness;
2. call `assign` with the supervisor's terminal identity;
3. wait for the worker to process its task;
4. send a callback through the worker's recorded caller relationship;
5. prove inbox delivery and the supervisor's next idle/completed transition;
6. prove a missing receiver fails with categorical evidence;
7. delete the worker, remove the session, and stop the server; and
8. assert no terminal or inbox rows remain and the server listener is closed.

It uses no model API, credentials, user CAO state, or network service. CI runs
it in a dedicated job with `tmux` installed:

```bash
uv run pytest test/integration/test_supervisor_harness.py -v -o addopts=''
```

The fake lane validates CAO's lifecycle and message-routing substrate. It does
not claim that a vendor CLI version, exact model, or authentication session is
healthy.

## Exact-provider live matrix

[`test/e2e/test_exact_provider_supervisor_matrix.py`](../test/e2e/test_exact_provider_supervisor_matrix.py)
is opt-in and launches one supervisor lane for each supported provider:

| Lane | Provider | Exact model |
|---|---|---|
| Codex | `codex` | `gpt-5.6-sol` |
| Claude Code | `claude_code` | `claude-opus-5` |
| Antigravity | `antigravity_cli` | `gemini-3.1-pro-high` |
| Kimi | `kimi_cli` | `kimi-code/k3` |

Each supervisor must assign exactly one same-provider worker, become idle for
the callback, receive the worker result through the authoritative inbox, delete
the worker, and emit `CAO_LIVE_FAN_IN_OK`. The harness runs every lane once,
does not relaunch failed lanes, and emits one categorical fan-in verdict after
all lanes have been attempted.

The live matrix starts its own source-backed server with an isolated Herdr
session and home. It never uses the operator's active CAO session. Its teardown
stops and deletes only the generated Herdr session.

### Strict preflight

Setting `CAO_LIVE_SUPERVISOR_MATRIX=1` opts into real model calls. Before the
isolated server starts, the Pydantic-validated preflight in
[`test/harness/live_supervisor_matrix.py`](../test/harness/live_supervisor_matrix.py)
requires:

- enough free disk capacity (5 GiB by default);
- `herdr`, `codex`, `claude`, `agy`, and `kimi` binaries;
- a loopback `CAO_OPS_MCP_URL` whose path is exactly `/mcp/ops`;
- a loopback `CAO_SERENA_MCP_URL` without URL userinfo;
- exact Antigravity and Kimi model-catalog matches; and
- a successful authenticated probe against every exact model.

Failed probes retain only component and reason codes. Command output, resolved
URLs, credentials, and model responses are not included in the fan-in error.
Aliases such as `opus`, `latest`, or `default` are rejected because they can
silently change models.

Run only after the embedded CAO Ops endpoint and Serena daemon are healthy:

```bash
export CAO_LIVE_SUPERVISOR_MATRIX=1
export CAO_SERENA_MCP_URL=http://127.0.0.1:8765/mcp
uv run pytest -m e2e \
  test/e2e/test_exact_provider_supervisor_matrix.py \
  -v -o addopts=''
```

The harness injects the isolated server's `/mcp/ops` URL itself. The preflight
model calls are intentional and may incur provider usage. A missing binary,
expired authentication session, unavailable exact model, malformed MCP URL,
Herdr startup failure, or disk-capacity failure is a failed matrix—not a
provider skip or model downgrade.

## Supervisor lifecycle contract

The harness encodes the same lifecycle expected in development and adversarial
review swarms:

- the supervisor owns every worker from assignment through deletion;
- the supervisor becomes idle after asynchronous assignment so callbacks can
  arrive;
- inbox delivery, not a resource notification, is the completion authority;
- model/provider identifiers are exact and immutable for the run;
- failures are reported immediately and categorically;
- no lane is automatically relaunched; and
- emergency test teardown removes only resources bearing the generated test
  session identity.

"""MCP approval policy must not exceed the profile's declared permission mode."""

import shlex
import json
import subprocess
import sys
import tomllib
import venv
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.codex import CodexProvider, ProviderError


@pytest.fixture(autouse=True)
def _isolated_codex_home(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path)


def _build(mode, server, overrides=None):
    profile = AgentProfile(
        name="test",
        description="test",
        system_prompt="",
        permissionMode=mode,
        mcpServers={"external": server},
        codexConfig=overrides,
    )
    with patch("cli_agent_orchestrator.providers.codex.load_agent_profile", return_value=profile):
        return shlex.split(
            CodexProvider("tid", "s", "w", "test", allowed_tools=["*"])._build_codex_command()
        )


SERVERS = [
    pytest.param({"type": "http", "url": "https://example.invalid/mcp"}, id="http"),
    pytest.param({"command": "external-mcp", "args": []}, id="stdio"),
]


@pytest.mark.parametrize("server", SERVERS)
@pytest.mark.parametrize("mode", [None, "default", "plan", "acceptEdits", "bypassPermissions"])
def test_mcp_approval_requires_explicit_bypass(mode, server):
    argv = _build(mode, server)
    expected = "approve" if mode == "bypassPermissions" else "prompt"
    settings = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-c"]
    assert f'mcp_servers.external.default_tools_approval_mode="{expected}"' in settings
    if mode != "bypassPermissions":
        assert not any(setting.endswith('="approve"') for setting in settings)


@pytest.mark.parametrize("server", SERVERS)
@pytest.mark.parametrize("mode", [None, "default", "plan", "acceptEdits"])
@pytest.mark.parametrize(
    "key",
    [
        "mcp_servers.external.default_tools_approval_mode",
        "mcp_servers.external.tools.mutate.approval_mode",
        "mcp_servers.other.tools.mutate.approval_mode",
    ],
)
@pytest.mark.parametrize("value", ["approve", "auto", "writes"])
def test_inline_mcp_override_cannot_elevate_restricted_profile(mode, server, key, value):
    with pytest.raises(ProviderError, match="MCP approval overrides require prompt"):
        _build(mode, server, {key: value})


@pytest.mark.parametrize("mode", ["plan", "acceptEdits", "bypassPermissions"])
def test_prompt_override_and_unrelated_config_remain_supported(mode):
    argv = _build(
        mode,
        {"command": "external-mcp"},
        {
            "mcp_servers.external.tools.mutate.approval_mode": "prompt",
            "model_reasoning_effort": "high",
        },
    )
    assert 'mcp_servers.external.tools.mutate.approval_mode="prompt"' in argv
    assert 'model_reasoning_effort="high"' in argv


def test_explicit_bypass_permits_inline_approve_override():
    argv = _build(
        "bypassPermissions",
        {"command": "external-mcp"},
        {
            "mcp_servers.external.tools.mutate.approval_mode": "approve",
        },
    )
    assert 'mcp_servers.external.tools.mutate.approval_mode="approve"' in argv


def _callback_command(mode, server, grants, effective, resolved=None):
    profile = AgentProfile(
        name="worker",
        description="worker",
        system_prompt="",
        permissionMode=mode,
        allowedTools=grants,
        mcpServers={"cao-mcp-server": server},
    )
    with (
        patch("cli_agent_orchestrator.providers.codex.load_agent_profile", return_value=profile),
        patch(
            "cli_agent_orchestrator.utils.mcp_resolution._sibling_script",
            return_value=str(Path(sys.executable).with_name("cao-mcp-server")),
        ),
    ):
        provider = CodexProvider("tid", "s", "w", "worker", allowed_tools=effective)
        if resolved is not None:
            with patch(
                "cli_agent_orchestrator.providers.codex.resolve_mcp_server_config",
                return_value=resolved,
            ):
                return shlex.split(provider._build_codex_command())
        return shlex.split(provider._build_codex_command())


@pytest.mark.parametrize("mode", ["plan", "acceptEdits"])
@pytest.mark.parametrize("grant", ["@cao-mcp-server", "mcp__cao-mcp-server__send_message"])
def test_managed_worker_approves_only_explicit_send_message_callback(mode, grant):
    argv = _callback_command(mode, {"command": "cao-mcp-server", "args": []}, [grant], [grant])
    approvals = [arg for arg in argv if arg.endswith('approval_mode="approve"')]
    assert approvals == ['mcp_servers.cao-mcp-server.tools.send_message.approval_mode="approve"']
    assert 'mcp_servers.cao-mcp-server.default_tools_approval_mode="prompt"' in argv
    assert not any(
        f".tools.{tool}." in arg for arg in argv for tool in ("assign", "handoff", "cli_exec")
    )


@pytest.mark.parametrize(
    "server",
    [
        {"command": "/custom/cao-mcp-server"},
        {"command": "custom-wrapper", "args": ["cao-mcp-server"]},
        {"command": "cao-mcp-server", "args": ["--custom"]},
        {"command": "cao-mcp-server", "env": {"PYTHONPATH": "/untrusted"}},
        {"type": "http", "url": "http://127.0.0.1:9889/mcp/ops"},
        {"type": "http", "url": "https://example.invalid/mcp"},
    ],
)
def test_server_label_cannot_grant_callback_approval_to_custom_transport(server):
    argv = _callback_command("plan", server, ["@cao-mcp-server"], ["@cao-mcp-server"])
    assert not any(arg.endswith('approval_mode="approve"') for arg in argv)


@pytest.mark.parametrize(
    "mode,grants,effective",
    [
        ("default", ["@cao-mcp-server"], ["@cao-mcp-server"]),
        ("plan", ["*"], ["*"]),
        ("plan", [], ["@cao-mcp-server"]),
        ("plan", ["@other"], ["@cao-mcp-server"]),
        ("plan", ["@cao-mcp-server"], []),
        ("plan", ["@cao-mcp-server"], ["fs_read"]),
    ],
)
def test_callback_needs_explicit_profile_grant_and_effective_permission(mode, grants, effective):
    argv = _callback_command(mode, {"command": "cao-mcp-server"}, grants, effective)
    assert not any(arg.endswith('approval_mode="approve"') for arg in argv)


def test_path_fallback_is_not_trusted_for_callback_approval():
    argv = _callback_command(
        "plan",
        {"command": "cao-mcp-server"},
        ["@cao-mcp-server"],
        ["@cao-mcp-server"],
        resolved={"command": "/custom/path/cao-mcp-server", "args": []},
    )
    assert not any(arg.endswith('approval_mode="approve"') for arg in argv)


def test_managed_module_callback_isolated_from_worker_import_path():
    module = "cli_agent_orchestrator.mcp_server.server"
    argv = _callback_command(
        "plan",
        {"command": "cao-mcp-server"},
        ["@cao-mcp-server"],
        ["@cao-mcp-server"],
        resolved={"command": sys.executable, "args": ["-m", module]},
    )
    setting = next(arg for arg in argv if arg.startswith("mcp_servers.cao-mcp-server.args="))
    args = tomllib.loads(setting)["mcp_servers"]["cao-mcp-server"]["args"]
    assert args[:2] == ["-I", "-c"]
    assert 'mcp_servers.cao-mcp-server.tools.send_message.approval_mode="approve"' in argv


def test_source_only_module_bootstrap_ignores_hostile_cwd_and_pythonpath(tmp_path, monkeypatch):
    from cli_agent_orchestrator.providers.codex import _managed_mcp_module_args

    # A clean interpreter has no CAO package or console script installed.
    clean_env = tmp_path / "clean-python"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(clean_env)
    interpreter = clean_env / "bin" / "python"
    trusted_root = tmp_path / "trusted source"
    package = trusted_root / "cli_agent_orchestrator"
    (package / "mcp_server").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "mcp_server" / "__init__.py").write_text("")
    (package / "mcp_server" / "server.py").write_text(
        "if __name__ == '__main__':\n    print('TRUSTED_SOURCE_MAIN')\n"
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.codex.__file__", str(package / "providers" / "codex.py")
    )
    hostile = tmp_path / "hostile"
    (hostile / "cli_agent_orchestrator").mkdir(parents=True)
    marker = tmp_path / "hijacked"
    (hostile / "cli_agent_orchestrator" / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('hijacked')\n"
    )
    env = {"HOME": str(tmp_path), "PYTHONPATH": str(hostile), "PATH": str(clean_env / "bin")}
    source_env = {**env, "PYTHONPATH": str(trusted_root)}
    baseline = subprocess.run(
        [str(interpreter), "-m", "cli_agent_orchestrator.mcp_server.server"],
        cwd=tmp_path,
        env=source_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert baseline.returncode == 0, baseline.stderr
    assert baseline.stdout.strip() == "TRUSTED_SOURCE_MAIN"
    previous = subprocess.run(
        [str(interpreter), "-I", "-m", "cli_agent_orchestrator.mcp_server.server"],
        cwd=tmp_path,
        env=source_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert previous.returncode != 0  # reproduces the source-only -I -m failure
    launched = subprocess.run(
        [str(interpreter), *_managed_mcp_module_args()],
        cwd=hostile,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout.strip() == "TRUSTED_SOURCE_MAIN"
    assert not marker.exists()


def test_current_source_module_bootstrap_completes_mcp_initialize(tmp_path):
    from cli_agent_orchestrator.providers.codex import _managed_mcp_module_args

    hostile = tmp_path / "worker"
    (hostile / "cli_agent_orchestrator").mkdir(parents=True)
    marker = tmp_path / "hijacked"
    (hostile / "cli_agent_orchestrator" / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('hijacked')\n"
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    process = subprocess.run(
        [sys.executable, *_managed_mcp_module_args()],
        cwd=hostile,
        env={
            "HOME": str(tmp_path),
            "CAO_HOME_DIR": str(tmp_path / "cao"),
            "PYTHONPATH": str(hostile),
            "PATH": str(Path(sys.executable).parent),
        },
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    messages = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
    assert any(message.get("id") == 1 and "result" in message for message in messages)
    assert not marker.exists()


@pytest.mark.parametrize("tool", ["assign", "handoff"])
@pytest.mark.parametrize("mode", ["plan", "acceptEdits"])
@pytest.mark.parametrize("effective", ["exact", "server", "wildcard", "empty", "other"])
def test_managed_fanout_requires_exact_grants_in_both_layers(tool, mode, effective):
    grant = f"mcp__cao-mcp-server__{tool}"
    effective_tools = {
        "exact": [grant],
        "server": ["@cao-mcp-server"],
        "wildcard": ["*"],
        "empty": [],
        "other": ["mcp__other__assign"],
    }[effective]
    argv = _callback_command(mode, {"command": "cao-mcp-server"}, [grant], effective_tools)
    approval = f'mcp_servers.cao-mcp-server.tools.{tool}.approval_mode="approve"'
    assert (approval in argv) is (effective == "exact")
    assert 'mcp_servers.cao-mcp-server.default_tools_approval_mode="prompt"' in argv


@pytest.mark.parametrize("declared", [["@cao-mcp-server"], ["*"], []])
def test_effective_fanout_does_not_override_missing_profile_grant(declared):
    argv = _callback_command(
        "plan",
        {"command": "cao-mcp-server"},
        declared,
        ["mcp__cao-mcp-server__assign", "mcp__cao-mcp-server__handoff"],
    )
    assert not any(arg.endswith('approval_mode="approve"') for arg in argv)


@pytest.mark.parametrize(
    "server",
    [
        {"command": "/custom/cao-mcp-server"},
        {"command": "wrapper", "args": ["cao-mcp-server"]},
        {"command": "cao-mcp-server", "env": {"PYTHONPATH": "/untrusted"}},
        {"type": "http", "url": "https://example.invalid/mcp"},
    ],
)
def test_explicit_fanout_grants_do_not_trust_custom_server(server):
    grants = ["mcp__cao-mcp-server__assign", "mcp__cao-mcp-server__handoff"]
    argv = _callback_command("plan", server, grants, grants)
    assert not any(arg.endswith('approval_mode="approve"') for arg in argv)


def test_arbitrary_managed_tool_is_not_approved_by_explicit_token():
    grant = "mcp__cao-mcp-server__memory_store"
    argv = _callback_command("plan", {"command": "cao-mcp-server"}, [grant], [grant])
    assert not any(arg.endswith('approval_mode="approve"') for arg in argv)


@pytest.mark.parametrize(
    "profile_name,expected_tools",
    [
        ("cao-repo-supervisor-sol", {"send_message", "assign", "handoff"}),
        ("cao-repo-execution-supervisor-sol", {"send_message", "assign", "handoff"}),
        ("cao-repo-review-codex-sol", {"send_message"}),
    ],
)
def test_shipped_codex_profiles_render_exact_managed_tools(
    monkeypatch, profile_name, expected_tools
):
    from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
    from cli_agent_orchestrator.utils.skills import InlineSkillPrompt

    repo_root = Path(__file__).resolve().parents[2]
    profile = load_agent_profile(profile_name, start=repo_root)
    monkeypatch.setenv("CAO_SERENA_MCP_URL", "https://example.invalid/mcp")
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)
    with (
        patch("cli_agent_orchestrator.providers.codex.load_agent_profile", return_value=profile),
        patch(
            "cli_agent_orchestrator.utils.mcp_resolution._sibling_script",
            return_value=str(Path(sys.executable).with_name("cao-mcp-server")),
        ),
    ):
        provider = CodexProvider(
            "shipped",
            "s",
            "w",
            profile_name,
            allowed_tools=list(profile.allowedTools),
            skill_prompt=InlineSkillPrompt(""),
        )
        argv = shlex.split(provider._build_codex_command())
    approvals = {arg for arg in argv if arg.endswith('approval_mode="approve"')}
    assert approvals == {
        f'mcp_servers.cao-mcp-server.tools.{tool}.approval_mode="approve"'
        for tool in expected_tools
    }
    assert 'mcp_servers.cao-mcp-server.default_tools_approval_mode="prompt"' in argv
    assert "--yolo" not in argv

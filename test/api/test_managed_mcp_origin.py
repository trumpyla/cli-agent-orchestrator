"""Managed MCP credentials follow the resolved server listener authority."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.api import main as api
from cli_agent_orchestrator.providers import mcp_translation as translation


@pytest.mark.parametrize(
    "cli_host,cli_port,client_host,effective_port",
    [
        (None, None, "127.0.0.1", 9897),
        ("0.0.0.0", 9999, "127.0.0.1", 9999),
        ("::", 9898, "[::1]", 9898),
        ("localhost", 9899, "localhost", 9899),
        ("cao.example.invalid", 9899, "cao.example.invalid", 9899),
    ],
)
def test_cli_origin_is_bound_before_uvicorn_starts(
    monkeypatch,
    cli_host,
    cli_port,
    client_host,
    effective_port,
):
    monkeypatch.setattr(translation, "_managed_cao_origin", None)
    monkeypatch.setattr(api, "SERVER_HOST", "127.0.0.1")
    monkeypatch.setattr(api, "SERVER_PORT", 9897)
    monkeypatch.setattr(
        "argparse.ArgumentParser.parse_args",
        lambda _self: SimpleNamespace(
            agents_dir=None,
            host=cli_host,
            port=cli_port,
            terminal=None,
        ),
    )
    monkeypatch.setattr(api, "add_local_cors_origins", lambda *_args: None)
    env = {
        "CAO_AUTH_JWKS_URI": "https://idp.example.invalid/jwks",
        "CAO_AUTH_LOCAL_TOKEN": "synthetic-origin-token",
        "CAO_API_HOST": "127.0.0.2",
        "CAO_API_PORT": "9888",
    }

    def serve(_app, **kwargs):
        assert kwargs["host"] == (cli_host or "127.0.0.1")
        assert kwargs["port"] == effective_port
        for provider in translation.HTTP_SUPPORTED_PROVIDERS:
            native = translation.render_http_entry(
                provider,
                f"http://{client_host}:{effective_port}/mcp/ops",
                env=env,
            )
            assert any(
                key in native for key in ("headers", "bearer_token_env_var", "bearerTokenEnvVar")
            )
            for denied in (
                f"http://127.0.0.2:{effective_port}/mcp/ops",
                f"http://{client_host}:9888/mcp/ops",
            ):
                native = translation.render_http_entry(provider, denied, env=env)
                assert not any(
                    key in native
                    for key in ("headers", "bearer_token_env_var", "bearerTokenEnvVar")
                )

    run = MagicMock(side_effect=serve)
    monkeypatch.setattr("uvicorn.run", run)
    api.main()
    run.assert_called_once()

"""Authentication and scope contracts for peer-inbox HTTP routes."""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.security import auth


def _override_scopes(scopes):
    async def _dep():
        return list(scopes)

    return _dep


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(auth.get_current_scopes, None)


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/terminals/deadbeef/inbox/messages", {}),
        ("post", "/peers", {"json": {}}),
        ("post", "/terminals/deadbeef/inbox/ack", {"json": {"message_ids": []}}),
    ],
)
def test_peer_routes_require_credentials_when_auth_enabled(client, auth_on, method, path, kwargs):
    with (
        patch("cli_agent_orchestrator.api.main.get_inbox_messages", return_value=[]),
        patch("cli_agent_orchestrator.api.main.create_peer", return_value="deadbeef"),
        patch("cli_agent_orchestrator.api.main.is_peer", return_value=True),
        patch("cli_agent_orchestrator.api.main.mark_messages_delivered", return_value=0),
    ):
        response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401


def test_read_scope_can_read_but_cannot_register_or_ack(client, auth_on):
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([auth.SCOPE_READ])

    with patch("cli_agent_orchestrator.api.main.get_inbox_messages", return_value=[]):
        assert client.get("/terminals/deadbeef/inbox/messages").status_code == 200
    assert client.post("/peers", json={}).status_code == 403
    assert client.post("/terminals/deadbeef/inbox/ack", json={"message_ids": []}).status_code == 403


@pytest.mark.parametrize("scope", [auth.SCOPE_WRITE, auth.SCOPE_ADMIN])
def test_write_or_admin_scope_can_use_all_peer_routes(client, auth_on, scope):
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([scope])

    with patch("cli_agent_orchestrator.api.main.get_inbox_messages", return_value=[]):
        assert client.get("/terminals/deadbeef/inbox/messages").status_code == 200
    with patch("cli_agent_orchestrator.api.main.create_peer", return_value="deadbeef"):
        assert client.post("/peers", json={}).status_code == 200
    with (
        patch("cli_agent_orchestrator.api.main.is_peer", return_value=True),
        patch("cli_agent_orchestrator.api.main.mark_messages_delivered", return_value=0),
    ):
        assert (
            client.post("/terminals/deadbeef/inbox/ack", json={"message_ids": []}).status_code
            == 200
        )

"""Embedded CAO Ops Streamable HTTP and authentication contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from fastmcp.server.dependencies import get_http_headers

from cli_agent_orchestrator.api import main as api_main
from cli_agent_orchestrator.ops_mcp_server import server as ops_server
from cli_agent_orchestrator.ops_mcp_server.backend import AsgiRequestBackend


def test_embedded_ops_app_is_mounted_before_web_ui_catchall() -> None:
    """Mounting after ``/`` would make the MCP endpoint unreachable."""
    ops_http_app = getattr(api_main, "ops_http_app", None)
    route_paths = [getattr(route, "path", None) for route in api_main.app.routes]

    assert ops_http_app is not None, "embedded FastMCP HTTP app is missing"
    assert "/mcp" in route_paths
    if "/" in route_paths:
        assert route_paths.index("/mcp") < route_paths.index("/")


def test_cao_token_verifier_exists_for_stateful_mcp_authentication() -> None:
    """Without a verifier, MCP requests cannot bind sessions to principals."""
    assert hasattr(ops_server, "CaoTokenVerifier")


@dataclass(frozen=True)
class TokenIdentity:
    """Validated token facts used by the hermetic authentication fake."""

    scopes: list[str]
    issuer: str
    subject: str
    client_id: str


@dataclass
class EmbeddedStack:
    """One fresh stateful MCP app and its authoritative REST observations."""

    client: httpx.AsyncClient
    backend: AsgiRequestBackend
    rest_authorizations: list[str | None]


def _authorization_from_current_mcp_request() -> str | None:
    return get_http_headers(include={"authorization"}).get("authorization")


@asynccontextmanager
async def embedded_stack() -> AsyncIterator[EmbeddedStack]:
    """Build a fresh session manager per test so retained state cannot leak."""
    rest_authorizations: list[str | None] = []
    backend = AsgiRequestBackend(authorization=_authorization_from_current_mcp_request)
    mcp = ops_server.create_ops_mcp(
        backend,
        auth=ops_server.CaoTokenVerifier(),
        subscription_authorization=_authorization_from_current_mcp_request,
    )
    mcp_http = mcp.http_app(path="/ops", transport="http", stateless_http=False)
    host = FastAPI(lifespan=mcp_http.lifespan)
    host.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "testserver"],
    )

    @host.get("/sessions")
    async def list_sessions(request: Request) -> JSONResponse:
        rest_authorizations.append(request.headers.get("authorization"))
        return JSONResponse([{"session_name": "cao-one"}])

    @host.post("/sessions")
    async def launch_session(request: Request) -> JSONResponse:
        authorization = request.headers.get("authorization")
        rest_authorizations.append(authorization)
        token = authorization.removeprefix("Bearer ") if authorization else ""
        identity = _TOKEN_IDENTITIES.get(token)
        if identity is None or not {"cao:write", "cao:admin"}.intersection(identity.scopes):
            return JSONResponse(
                {"detail": "Forbidden: requires one of ['cao:write', 'cao:admin']"},
                status_code=403,
            )
        requested_name = request.query_params.get("session_name") or "cao-write"
        return JSONResponse(
            {"id": f"terminal-{requested_name}", "session_name": requested_name},
            status_code=201,
        )

    @host.get("/terminals/{peer_id}/inbox/messages")
    async def receive_inbox(peer_id: str, request: Request) -> JSONResponse:
        del peer_id
        rest_authorizations.append(request.headers.get("authorization"))
        return JSONResponse([])

    host.mount("/mcp", mcp_http)
    backend.bind(host)
    transport = httpx.ASGITransport(app=host)
    async with (
        host.router.lifespan_context(host),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            follow_redirects=False,
        ) as client,
    ):
        yield EmbeddedStack(
            client=client,
            backend=backend,
            rest_authorizations=rest_authorizations,
        )


@pytest.fixture
def stack_factory() -> Callable[[], AbstractAsyncContextManager[EmbeddedStack]]:
    return embedded_stack


_TOKEN_IDENTITIES: dict[str, TokenIdentity] = {}


@pytest.fixture
def enable_fake_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[dict[str, TokenIdentity]], None]:
    """Install strict, secret-free token validation for one test."""

    def _enable(identities: dict[str, TokenIdentity]) -> None:
        _TOKEN_IDENTITIES.clear()
        _TOKEN_IDENTITIES.update(identities)
        monkeypatch.setattr(ops_server, "is_auth_enabled", lambda: True)

        def _scopes(token: str) -> list[str]:
            identity = _TOKEN_IDENTITIES.get(token)
            if identity is None:
                raise ValueError("private JWKS diagnostic must be redacted")
            return list(identity.scopes)

        def _claims(token: str, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            identity = _TOKEN_IDENTITIES[token]
            return {
                "iss": identity.issuer,
                "sub": identity.subject,
                "client_id": identity.client_id,
                "exp": 4_102_444_800,
            }

        monkeypatch.setattr(ops_server, "extract_scopes_from_token", _scopes)
        monkeypatch.setattr(ops_server.jwt, "decode", _claims)

    yield _enable
    _TOKEN_IDENTITIES.clear()


def _mcp_headers(token: str | None = None, session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": "localhost",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _jsonrpc_body(response: httpx.Response) -> dict[str, Any]:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data_lines = [
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert data_lines, response.text
        return json.loads(data_lines[-1])
    return response.json()


async def _initialize(
    client: httpx.AsyncClient,
    *,
    token: str | None = None,
) -> tuple[httpx.Response, str | None]:
    response = await client.post(
        "/mcp/ops",
        headers=_mcp_headers(token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "core-test", "version": "1.0"},
            },
        },
    )
    return response, response.headers.get("mcp-session-id")


async def _notify_initialized(
    client: httpx.AsyncClient,
    session_id: str,
    token: str | None,
) -> httpx.Response:
    return await client.post(
        "/mcp/ops",
        headers=_mcp_headers(token, session_id),
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
    )


async def _subscribe_peer(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    peer_id: str = "deadbeef",
    token: str | None = None,
) -> httpx.Response:
    return await client.post(
        "/mcp/ops",
        headers=_mcp_headers(token, session_id),
        json={
            "jsonrpc": "2.0",
            "id": 90,
            "method": "resources/subscribe",
            "params": {"uri": f"cao://peers/{peer_id}/inbox"},
        },
    )


async def _assert_initialize_list_call(
    client: httpx.AsyncClient,
    *,
    request_id: int,
) -> str:
    """Exercise the retained-session protocol through one exact MCP endpoint."""
    initialized, session_id = await _initialize(client)
    assert initialized.status_code == 200
    assert initialized.history == []
    assert initialized.url.path == "/mcp/ops"
    assert session_id is not None
    assert _jsonrpc_body(initialized)["result"]["serverInfo"]["name"] == "cao-ops-mcp"

    notification = await _notify_initialized(client, session_id, None)
    assert notification.status_code in {200, 202}

    listed = await client.post(
        "/mcp/ops",
        headers=_mcp_headers(session_id=session_id),
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/list",
            "params": {},
        },
    )
    assert listed.status_code == 200
    tool_names = {tool["name"] for tool in _jsonrpc_body(listed)["result"]["tools"]}
    assert "list_sessions" in tool_names

    called = await client.post(
        "/mcp/ops",
        headers=_mcp_headers(session_id=session_id),
        json={
            "jsonrpc": "2.0",
            "id": request_id + 1,
            "method": "tools/call",
            "params": {"name": "list_sessions", "arguments": {}},
        },
    )
    assert called.status_code == 200
    call_result = _jsonrpc_body(called)["result"]
    assert call_result.get("isError", False) is False
    assert call_result["structuredContent"]["success"] is True
    return session_id


@pytest.mark.asyncio
async def test_streamable_http_delete_cancels_session_owned_peer_subscription(
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP DELETE must not retain a consumer or its session/auth context."""
    monkeypatch.setattr(ops_server, "is_auth_enabled", lambda: False)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def stalled_consumer(
        peer_id: str,
        session: Any,
        authorization: Any,
    ) -> None:
        del peer_id, session, authorization
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(ops_server, "_consume_inbox", stalled_consumer)
    async with stack_factory() as stack:
        initialized, session_id = await _initialize(stack.client)
        assert initialized.status_code == 200
        assert session_id is not None
        assert (await _notify_initialized(stack.client, session_id, None)).status_code in {200, 202}

        subscribed = await _subscribe_peer(stack.client, session_id)
        assert subscribed.status_code == 200
        await asyncio.wait_for(started.wait(), timeout=1)

        deleted = await stack.client.delete(
            "/mcp/ops",
            headers=_mcp_headers(session_id=session_id),
        )
        assert deleted.status_code == 200
        await asyncio.wait_for(stopped.wait(), timeout=1)


@pytest.mark.asyncio
async def test_host_lifespan_shutdown_cancels_all_peer_subscriptions(
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host shutdown must clean consumers even when the client omits DELETE."""
    monkeypatch.setattr(ops_server, "is_auth_enabled", lambda: False)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def stalled_consumer(
        peer_id: str,
        session: Any,
        authorization: Any,
    ) -> None:
        del peer_id, session, authorization
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(ops_server, "_consume_inbox", stalled_consumer)
    async with stack_factory() as stack:
        initialized, session_id = await _initialize(stack.client)
        assert initialized.status_code == 200
        assert session_id is not None
        assert (await _notify_initialized(stack.client, session_id, None)).status_code in {200, 202}
        assert (await _subscribe_peer(stack.client, session_id)).status_code == 200
        await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(stopped.wait(), timeout=1)


@pytest.mark.asyncio
async def test_exact_mcp_path_initializes_without_redirect_when_auth_disabled(
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing-slash route would return 307/308 to native MCP clients."""
    monkeypatch.setattr(ops_server, "is_auth_enabled", lambda: False)

    async with stack_factory() as stack:
        response, session_id = await _initialize(stack.client)

        assert response.status_code == 200
        assert response.history == []
        assert response.url.path == "/mcp/ops"
        assert session_id
        assert _jsonrpc_body(response)["result"]["serverInfo"]["name"] == "cao-ops-mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_auth_enabled_validates_every_mcp_http_method(
    method: str,
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
    enable_fake_auth: Callable[[dict[str, TokenIdentity]], None],
) -> None:
    """Leaving any transport method unguarded would bypass session authentication."""
    enable_fake_auth({})
    request_kwargs: dict[str, Any] = {"headers": _mcp_headers()}
    if method == "POST":
        request_kwargs["json"] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "core-test", "version": "1.0"},
            },
        }

    async with stack_factory() as stack:
        response = await stack.client.request(method, "/mcp/ops", **request_kwargs)

        assert response.status_code == 401
        assert "private JWKS diagnostic" not in response.text


@pytest.mark.asyncio
async def test_valid_token_without_cao_scope_is_forbidden(
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
    enable_fake_auth: Callable[[dict[str, TokenIdentity]], None],
) -> None:
    """Treating any valid OAuth token as CAO-authorized would escalate access."""
    enable_fake_auth(
        {
            "unscoped": TokenIdentity(
                scopes=["profile:read"],
                issuer="https://issuer-a.test/",
                subject="subject-a",
                client_id="client-a",
            )
        }
    )

    async with stack_factory() as stack:
        response, session_id = await _initialize(stack.client, token="unscoped")

        assert response.status_code == 403
        assert session_id is None
        assert "unscoped" not in response.text


@pytest.mark.asyncio
async def test_invalid_token_response_and_logs_redact_token_and_jwks_details(
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
    enable_fake_auth: Callable[[dict[str, TokenIdentity]], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Surfacing verifier exceptions could disclose credentials or JWKS topology."""
    enable_fake_auth({})

    async with stack_factory() as stack:
        response, session_id = await _initialize(
            stack.client,
            token="secret-invalid-token",
        )

        combined = response.text + caplog.text
        assert response.status_code == 401
        assert session_id is None
        assert "secret-invalid-token" not in combined
        assert "private JWKS diagnostic" not in combined


@pytest.mark.asyncio
async def test_retained_session_accepts_refresh_and_rejects_other_principal(
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
    enable_fake_auth: Callable[[dict[str, TokenIdentity]], None],
) -> None:
    """Binding by raw token breaks refresh; omitting binding permits session replay."""
    enable_fake_auth(
        {
            "caller-old": TokenIdentity(
                scopes=["cao:read"],
                issuer="https://issuer-a.test/",
                subject="subject-a",
                client_id="client-a",
            ),
            "caller-refreshed": TokenIdentity(
                scopes=["cao:read"],
                issuer="https://issuer-a.test/",
                subject="subject-a",
                client_id="client-a",
            ),
            "other-principal": TokenIdentity(
                scopes=["cao:read"],
                issuer="https://issuer-a.test/",
                subject="subject-b",
                client_id="client-b",
            ),
        }
    )
    async with stack_factory() as stack:
        initialized, session_id = await _initialize(
            stack.client,
            token="caller-old",
        )
        assert initialized.status_code == 200
        assert session_id is not None
        notification = await _notify_initialized(
            stack.client,
            session_id,
            "caller-refreshed",
        )
        assert notification.status_code in {200, 202}

        refreshed = await stack.client.post(
            "/mcp/ops",
            headers=_mcp_headers("caller-refreshed", session_id),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        replay = await stack.client.post(
            "/mcp/ops",
            headers=_mcp_headers("other-principal", session_id),
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )

        assert refreshed.status_code == 200
        assert _jsonrpc_body(refreshed)["result"]["tools"]
        assert replay.status_code == 404
        assert _jsonrpc_body(replay)["error"]["message"] == "Session not found"


@pytest.mark.asyncio
async def test_current_refresh_header_reaches_rest_and_read_scope_stays_authoritative(
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
    enable_fake_auth: Callable[[dict[str, TokenIdentity]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reusing initialization or machine auth would bypass the REST scope boundary."""
    enable_fake_auth(
        {
            "old-read-token": TokenIdentity(
                scopes=["cao:read"],
                issuer="https://issuer-a.test/",
                subject="subject-a",
                client_id="client-a",
            ),
            "fresh-read-token": TokenIdentity(
                scopes=["cao:read"],
                issuer="https://issuer-a.test/",
                subject="subject-a",
                client_id="client-a",
            ),
        }
    )
    monkeypatch.setattr(
        ops_server,
        "get_local_bearer",
        lambda: (_ for _ in ()).throw(AssertionError("machine token was consulted")),
    )
    async with stack_factory() as stack:
        initialized, session_id = await _initialize(
            stack.client,
            token="old-read-token",
        )
        assert initialized.status_code == 200
        assert session_id is not None
        await _notify_initialized(
            stack.client,
            session_id,
            "fresh-read-token",
        )

        response = await stack.client.post(
            "/mcp/ops",
            headers=_mcp_headers("fresh-read-token", session_id),
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "launch_session",
                    "arguments": {
                        "agent_profile": "developer",
                        "session_name": "read-only-attempt",
                    },
                },
            },
        )

        body = _jsonrpc_body(response)
        assert response.status_code == 200
        assert body["result"]["structuredContent"]["success"] is False
        assert "Forbidden" in body["result"]["structuredContent"]["message"]
        assert stack.rest_authorizations == ["Bearer fresh-read-token"]


@pytest.mark.asyncio
async def test_concurrent_launch_calls_use_embedded_async_backend_and_forward_auth(
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
    enable_fake_auth: Callable[[dict[str, TokenIdentity]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three concurrent MCP launches must share the active embedded backend."""
    enable_fake_auth(
        {
            "launch-token": TokenIdentity(
                scopes=["cao:write"],
                issuer="https://issuer-a.test/",
                subject="subject-a",
                client_id="client-a",
            )
        }
    )
    monkeypatch.setattr(
        ops_server,
        "get_local_bearer",
        lambda: (_ for _ in ()).throw(AssertionError("machine token was consulted")),
    )

    async with stack_factory() as stack:
        initialized, session_id = await _initialize(stack.client, token="launch-token")
        assert initialized.status_code == 200
        assert session_id is not None
        assert (
            await _notify_initialized(stack.client, session_id, "launch-token")
        ).status_code in {200, 202}

        async def launch(worker_number: int) -> dict[str, Any]:
            worker_name = f"cao-worker-{worker_number}"
            response = await stack.client.post(
                "/mcp/ops",
                headers=_mcp_headers("launch-token", session_id),
                json={
                    "jsonrpc": "2.0",
                    "id": worker_number,
                    "method": "tools/call",
                    "params": {
                        "name": "launch_session",
                        "arguments": {
                            "agent_profile": "developer",
                            "provider": "mock_cli",
                            "session_name": worker_name,
                        },
                    },
                },
            )
            assert response.status_code == 200
            result = _jsonrpc_body(response)["result"]["structuredContent"]
            assert result["success"] is True
            assert result["session_name"] == worker_name
            assert result["terminal_id"] == f"terminal-{worker_name}"
            return result

        results = await asyncio.wait_for(
            asyncio.gather(*(launch(worker_number) for worker_number in range(3))),
            timeout=2,
        )

        assert [result["session_name"] for result in results] == [
            "cao-worker-0",
            "cao-worker-1",
            "cao-worker-2",
        ]
        assert len({result["terminal_id"] for result in results}) == 3
        assert stack.rest_authorizations == ["Bearer launch-token"] * 3


@pytest.mark.asyncio
async def test_subscription_worker_uses_subscribe_request_authorization(
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
    enable_fake_auth: Callable[[dict[str, TokenIdentity]], None],
) -> None:
    """Background polling must receive an explicit subscribe-request credential."""
    enable_fake_auth(
        {
            "subscription-token": TokenIdentity(
                scopes=["cao:read"],
                issuer="https://issuer-a.test/",
                subject="subject-a",
                client_id="client-a",
            )
        }
    )

    async with stack_factory() as stack:
        initialized, session_id = await _initialize(
            stack.client,
            token="subscription-token",
        )
        assert initialized.status_code == 200
        assert session_id is not None
        assert (
            await _notify_initialized(
                stack.client,
                session_id,
                "subscription-token",
            )
        ).status_code in {200, 202}

        subscribed = await _subscribe_peer(
            stack.client,
            session_id,
            token="subscription-token",
        )
        assert subscribed.status_code == 200

        async def wait_for_poll() -> None:
            while not stack.rest_authorizations:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_poll(), timeout=1)
        assert stack.rest_authorizations[0] == "Bearer subscription-token"

        deleted = await stack.client.delete(
            "/mcp/ops",
            headers=_mcp_headers("subscription-token", session_id),
        )
        assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_application_lifespan_reentry_uses_fresh_mcp_state_and_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reusing a closed stateful manager breaks the second host lifecycle."""
    monkeypatch.setattr(ops_server, "is_auth_enabled", lambda: False)
    backend_instances: list[AsgiRequestBackend] = []
    backend_type = api_main.AsgiRequestBackend

    class RecordingBackend(backend_type):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            backend_instances.append(self)

    monkeypatch.setattr(api_main, "AsgiRequestBackend", RecordingBackend)

    first_session_id: str | None = None
    for lifecycle_entry in range(2):
        async with api_main.app.router.lifespan_context(api_main.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api_main.app),
                base_url="http://localhost",
                follow_redirects=False,
            ) as client:
                if first_session_id is not None:
                    stale = await client.post(
                        "/mcp/ops",
                        headers=_mcp_headers(session_id=first_session_id),
                        json={
                            "jsonrpc": "2.0",
                            "id": 50,
                            "method": "tools/list",
                            "params": {},
                        },
                    )
                    assert stale.status_code == 404
                    assert _jsonrpc_body(stale)["error"]["message"] == "Session not found"

                session_id = await _assert_initialize_list_call(
                    client,
                    request_id=10 + lifecycle_entry * 10,
                )
                if first_session_id is None:
                    first_session_id = session_id

    assert len(backend_instances) == 2
    assert backend_instances[0] is not backend_instances[1]
    for backend in backend_instances:
        assert backend._client is not None
        assert backend._client.is_closed is True


def test_existing_host_surfaces_keep_health_trusted_host_and_websocket_routes() -> None:
    """Embedding must not displace existing host middleware or routes."""
    client = TestClient(api_main.app)

    assert client.get("/health", headers={"Host": "localhost"}).status_code == 200
    assert client.get("/health", headers={"Host": "attacker.test"}).status_code == 400
    with patch(
        "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
        return_value=[],
    ):
        rest_response = client.get("/agents/profiles", headers={"Host": "localhost"})
    assert rest_response.status_code == 200
    assert rest_response.json() == []
    if (api_main.WEB_DIST / "index.html").exists():
        assert client.get("/", headers={"Host": "localhost"}).status_code == 200
    assert any(
        getattr(route, "path", None) == "/terminals/{terminal_id}/ws"
        for route in api_main.app.routes
    )


@pytest.mark.asyncio
async def test_mcp_lifespan_startup_failure_propagates_without_degraded_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed first start must close its backend and permit a fresh retry."""
    monkeypatch.setattr(ops_server, "is_auth_enabled", lambda: False)
    backend_instances: list[AsgiRequestBackend] = []
    backend_type = api_main.AsgiRequestBackend

    class FailFirstStartBackend(backend_type):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            backend_instances.append(self)

        async def start(self) -> None:
            if self is backend_instances[0]:
                raise RuntimeError("session manager failed")

    monkeypatch.setattr(api_main, "AsgiRequestBackend", FailFirstStartBackend)

    with pytest.raises(RuntimeError, match="session manager failed"):
        async with api_main.app.router.lifespan_context(api_main.app):
            raise AssertionError("startup failure was swallowed")

    assert len(backend_instances) == 1
    assert backend_instances[0]._client is not None
    assert backend_instances[0]._client.is_closed is True

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_main.app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        unavailable, session_id = await _initialize(client)
    assert unavailable.status_code == 503
    assert session_id is None

    async with api_main.app.router.lifespan_context(api_main.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_main.app),
            base_url="http://localhost",
            follow_redirects=False,
        ) as client:
            await _assert_initialize_list_call(client, request_id=70)

    assert len(backend_instances) == 2
    assert backend_instances[0] is not backend_instances[1]
    assert backend_instances[1]._client is not None
    assert backend_instances[1]._client.is_closed is True


@pytest.mark.asyncio
async def test_embedded_backend_closes_with_mcp_lifespan(
    stack_factory: Callable[[], AbstractAsyncContextManager[EmbeddedStack]],
) -> None:
    """Failing to close the shared ASGI client would leak its connection pool."""
    async with stack_factory() as stack:
        client = stack.backend._client
        assert client is not None
        assert client.is_closed is False

    assert client.is_closed is True

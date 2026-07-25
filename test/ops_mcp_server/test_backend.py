"""Contract tests for CAO Ops asynchronous request backends."""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cli_agent_orchestrator.ops_mcp_server import backend as backend_module

CLIENT_DEFAULT_TIMEOUT = backend_module.CLIENT_DEFAULT_TIMEOUT
AsgiRequestBackend = getattr(backend_module, "AsgiRequestBackend", None)
HttpxRequestBackend = getattr(backend_module, "HttpxRequestBackend", None)


def test_async_backend_protocol_exists_for_nonblocking_mcp_handlers() -> None:
    """Deleting the async boundary would let MCP handlers regress to requests."""
    try:
        backend_module = importlib.import_module("cli_agent_orchestrator.ops_mcp_server.backend")
    except ModuleNotFoundError:
        backend_module = None

    assert backend_module is not None, "the typed async backend module is missing"
    assert hasattr(backend_module, "AsyncRequestBackend")


def test_backend_implementations_exist_for_stdio_and_embedded_http() -> None:
    """Omitting either implementation would leave one MCP transport blocking."""
    assert HttpxRequestBackend is not None, "shared HTTPX backend is missing"
    assert AsgiRequestBackend is not None, "in-process ASGI backend is missing"


@dataclass(frozen=True)
class CapturedRequest:
    """One strict fake-client request, retained for literal assertions."""

    method: str
    url: str
    kwargs: dict[str, Any]


class StrictAsyncClient:
    """Small async fake that rejects unexpected calls through its signature."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[CapturedRequest] = []
        self.closed = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None | httpx.Timeout = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        if not self._responses:
            raise AssertionError("unexpected extra HTTP request")
        kwargs: dict[str, Any] = {
            "params": params,
            "json": json,
            "headers": headers,
        }
        if timeout is not httpx.USE_CLIENT_DEFAULT:
            kwargs["timeout"] = timeout
        self.requests.append(CapturedRequest(method=method, url=url, kwargs=kwargs))
        return self._responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class BlockingAsyncClient:
    """Deterministic in-flight client used to prove cancellation propagation."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        del method, url, kwargs
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        return None


def _json_response(
    payload: Any = None,
    *,
    status_code: int = 200,
    content: bytes | None = None,
) -> httpx.Response:
    request = httpx.Request("GET", "http://cao.test/rest")
    if content is not None:
        return httpx.Response(status_code, content=content, request=request)
    return httpx.Response(status_code, json=payload, request=request)


@pytest.mark.asyncio
async def test_httpx_backend_reuses_one_client_and_forwards_local_bearer() -> None:
    """Recreating clients per call would lose pooling and local bearer policy."""
    client = StrictAsyncClient([_json_response({"n": 1}), _json_response({"n": 2})])
    backend = HttpxRequestBackend(
        base_url="http://cao.test",
        client=client,
        authorization=lambda: "Bearer machine-token",
    )

    first = await backend.request_json("get", "/sessions", operation="List sessions")
    second = await backend.request_json("get", "/sessions", operation="List sessions")
    await backend.aclose()

    assert first == ({"n": 1}, None)
    assert second == ({"n": 2}, None)
    assert [call.kwargs["headers"] for call in client.requests] == [
        {"Authorization": "Bearer machine-token"},
        {"Authorization": "Bearer machine-token"},
    ]
    assert client.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout", "expected_timeout", "expects_keyword"),
    [
        pytest.param(CLIENT_DEFAULT_TIMEOUT, None, False, id="ordinary-client-default"),
        pytest.param(None, None, True, id="immediate-no-timeout"),
        pytest.param(65.0, 65.0, True, id="long-poll-plus-five"),
    ],
)
async def test_httpx_backend_preserves_explicit_timeout_contract(
    timeout: object,
    expected_timeout: float | None,
    expects_keyword: bool,
) -> None:
    """Dropping explicit None or wait+5 would reintroduce HTTPX's 5s timeout."""
    client = StrictAsyncClient([_json_response([])])
    backend = HttpxRequestBackend(base_url="http://cao.test", client=client)

    await backend.request_json(
        "get",
        "/terminals/deadbeef/inbox/messages",
        operation="Receive messages",
        timeout=timeout,
    )

    kwargs = client.requests[0].kwargs
    assert ("timeout" in kwargs) is expects_keyword
    if expects_keyword:
        assert kwargs["timeout"] == expected_timeout


@pytest.mark.asyncio
async def test_httpx_backend_maps_http_and_invalid_json_failures() -> None:
    """Bypassing the shared mapper would drift stdio and embedded error shapes."""
    client = StrictAsyncClient(
        [
            _json_response({"detail": "denied"}, status_code=403),
            _json_response(content=b"not-json"),
        ]
    )
    backend = HttpxRequestBackend(base_url="http://cao.test", client=client)

    forbidden = await backend.request_json("post", "/sessions", operation="Launch session")
    malformed = await backend.request_json("get", "/sessions", operation="List sessions")

    assert forbidden == (None, "Launch session failed: denied")
    assert malformed[0] is None
    assert malformed[1] is not None
    assert malformed[1].startswith("List sessions failed: invalid JSON response (")


@pytest.mark.asyncio
async def test_httpx_backend_propagates_inflight_cancellation() -> None:
    """Catching CancelledError would strand a long-poll after its MCP call ends."""
    client = BlockingAsyncClient()
    backend = HttpxRequestBackend(base_url="http://cao.test", client=client)
    request_task = asyncio.create_task(
        backend.request_json(
            "get",
            "/terminals/deadbeef/inbox/messages",
            operation="Receive messages",
            timeout=65.0,
        )
    )
    await client.started.wait()

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert client.cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        pytest.param("http://attacker.test/sessions", id="absolute-url"),
        pytest.param("//attacker.test/sessions", id="scheme-relative-url"),
        pytest.param("/mcp/ops", id="recursive-mcp"),
        pytest.param("/health", id="non-owned-health"),
        pytest.param("/agents/providers", id="non-owned-rest"),
        pytest.param("/sessions/../mcp/ops", id="path-traversal"),
    ],
)
async def test_asgi_backend_rejects_non_owned_paths_before_dispatch(path: str) -> None:
    """A widened path boundary could recurse into MCP or reach unrelated routes."""
    entered_asgi = False

    async def sessions_endpoint(request: Request) -> JSONResponse:
        nonlocal entered_asgi
        entered_asgi = True
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/sessions", sessions_endpoint)])
    backend = AsgiRequestBackend(app=app)

    data, error = await backend.request_json("get", path, operation="Unsafe call")

    assert data is None
    assert error == "Unsafe call failed: embedded REST path is not allowed"
    assert entered_asgi is False


@pytest.mark.asyncio
async def test_asgi_backend_dispatches_owned_path_with_exact_current_authorization() -> None:
    """Using a machine or initialization token would break caller isolation."""
    seen_authorization: list[str | None] = []

    async def sessions_endpoint(request: Request) -> JSONResponse:
        seen_authorization.append(request.headers.get("authorization"))
        return JSONResponse([{"name": "cao-one"}])

    app = Starlette(routes=[Route("/sessions", sessions_endpoint)])
    backend = AsgiRequestBackend(
        app=app,
        authorization=lambda: "Bearer refreshed-caller-token",
    )

    result = await backend.request_json("get", "/sessions", operation="List sessions")
    await backend.aclose()

    assert result == ([{"name": "cao-one"}], None)
    assert seen_authorization == ["Bearer refreshed-caller-token"]

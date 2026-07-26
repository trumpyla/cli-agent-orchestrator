"""Asynchronous request boundary for CAO Ops MCP handlers."""

from __future__ import annotations

import re
from collections.abc import Callable
from enum import Enum
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

import httpx
from starlette.types import ASGIApp


class ClientDefaultTimeout(Enum):
    """Sentinel selecting the shared HTTP client's configured timeout."""

    VALUE = "client-default"


CLIENT_DEFAULT_TIMEOUT = ClientDefaultTimeout.VALUE
RequestTimeout = float | None | ClientDefaultTimeout


class RequestFailure(str):
    """Backward-compatible request error carrying an optional HTTP status."""

    status_code: int | None

    def __new__(
        cls,
        message: str,
        *,
        status_code: int | None = None,
    ) -> "RequestFailure":
        instance = str.__new__(cls, message)
        instance.status_code = status_code
        return instance


RequestResult = tuple[Any | None, str | None]


class AsyncRequestBackend(Protocol):
    """Typed asynchronous boundary from CAO Ops tools to authoritative REST."""

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        operation: str,
        timeout: RequestTimeout = CLIENT_DEFAULT_TIMEOUT,
    ) -> RequestResult: ...

    async def aclose(self) -> None: ...


class _AsyncHttpClient(Protocol):
    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...

    async def aclose(self) -> None: ...


AuthorizationProvider = Callable[[], str | None]


async def _request_json(
    client: _AsyncHttpClient | httpx.AsyncClient,
    authorization_provider: AuthorizationProvider | None,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None,
    json: Any | None,
    operation: str,
    timeout: RequestTimeout,
) -> RequestResult:
    request_kwargs: dict[str, Any] = {"params": params, "json": json}
    if authorization_provider is not None:
        authorization = authorization_provider()
        if authorization:
            request_kwargs["headers"] = {"Authorization": authorization}
    if timeout is not CLIENT_DEFAULT_TIMEOUT:
        request_kwargs["timeout"] = timeout

    try:
        response = await client.request(method, path, **request_kwargs)
    except httpx.RequestError as exc:
        return None, RequestFailure(f"{operation} failed: {exc}")
    return _map_response(response, operation)


class HttpxRequestBackend:
    """Shared HTTPX backend for the standalone stdio server."""

    def __init__(
        self,
        *,
        base_url: str,
        client: _AsyncHttpClient | None = None,
        authorization: AuthorizationProvider | None = None,
    ) -> None:
        self._client: _AsyncHttpClient | httpx.AsyncClient
        if client is None:
            self._client = httpx.AsyncClient(base_url=base_url)
        else:
            self._client = client
        self._authorization = authorization

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        operation: str,
        timeout: RequestTimeout = CLIENT_DEFAULT_TIMEOUT,
    ) -> RequestResult:
        return await _request_json(
            self._client,
            self._authorization,
            method,
            path,
            params=params,
            json=json,
            operation=operation,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class AsgiRequestBackend:
    """In-process HTTPX/ASGI backend for embedded MCP calls."""

    _OWNED_REST_PATH = re.compile(
        r"^/(?:"
        r"agents/profiles(?:/install|/[^/?#]+)?|"
        r"sessions(?:/[^/?#]+)?|"
        r"peers|"
        r"terminals/[^/?#]+(?:/(?:inbox/messages|inbox/ack|input|key|output))?"
        r")$"
    )

    def __init__(
        self,
        *,
        app: ASGIApp | None = None,
        authorization: AuthorizationProvider | None = None,
    ) -> None:
        self._client: httpx.AsyncClient | None = None
        self._authorization = authorization
        if app is not None:
            self.bind(app)

    def bind(self, app: ASGIApp) -> None:
        """Bind the backend after the host application has been constructed."""
        if self._client is not None:
            raise RuntimeError("embedded backend is already bound")
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        )

    @classmethod
    def _is_owned_rest_path(cls, path: str) -> bool:
        split = urlsplit(path)
        if split.scheme or split.netloc or split.query or split.fragment:
            return False
        if not path.startswith("/") or path.startswith("//"):
            return False
        decoded = unquote(path)
        if "\\" in decoded or any(segment in {".", ".."} for segment in decoded.split("/")):
            return False
        return cls._OWNED_REST_PATH.fullmatch(decoded) is not None

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        operation: str,
        timeout: RequestTimeout = CLIENT_DEFAULT_TIMEOUT,
    ) -> RequestResult:
        if not self._is_owned_rest_path(path):
            return None, RequestFailure(f"{operation} failed: embedded REST path is not allowed")
        if self._client is None:
            return None, RequestFailure(f"{operation} failed: embedded REST backend is not bound")

        return await _request_json(
            self._client,
            self._authorization,
            method,
            path,
            params=params,
            json=json,
            operation=operation,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message")
        if isinstance(detail, str) and detail:
            return detail

    text = response.text.strip()
    return text or f"HTTP {response.status_code}"


def _map_response(response: httpx.Response, operation: str) -> RequestResult:
    if response.status_code >= 400:
        return None, RequestFailure(
            f"{operation} failed: {_response_detail(response)}",
            status_code=response.status_code,
        )
    try:
        return response.json(), None
    except ValueError as exc:
        return None, RequestFailure(f"{operation} failed: invalid JSON response ({exc})")

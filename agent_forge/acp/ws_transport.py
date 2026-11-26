"""Authenticated FastAPI WebSocket/HTTP transport for ACP and workspace companion API."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse
except ImportError as exc:
    msg = (
        "ACP WebSocket/HTTP transport requires optional dependencies. "
        "Install with: pip install 'agent-forge[service]'"
    )
    raise ImportError(msg) from exc

from pydantic import BaseModel, ConfigDict, Field

from agent_forge import __version__
from agent_forge.acp.terminal import WorkspaceTerminalSession
from agent_forge.acp.workspace import PayloadTooLargeError, WorkspaceBridge

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agent_forge.acp.server import ACPServer

_logger = logging.getLogger(__name__)

DEFAULT_TOKEN_ENV_VAR = "AGENT_FORGE_ACP_TOKEN"  # noqa: S105 — env var name, not a secret
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)
_FORBIDDEN_QUERY_TOKEN_KEYS = frozenset({"token", "api_key", "access_token", "auth"})
_CSP_POLICY = (
    "default-src 'self'; script-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'self'"
)


class WriteFileBody(BaseModel):
    """Request body for writing a workspace file."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    content: str


@dataclass
class _SessionRecord:
    expires_at: float
    csrf_token: str


class SlidingWindowRateLimiter:
    """In-memory per-client sliding-window request rate limiter."""

    def __init__(self, *, max_requests: int = 120, window_seconds: float = 60.0) -> None:
        self._max_requests = max(1, max_requests)
        self._window_seconds = max(1.0, window_seconds)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, client_key: str) -> bool:
        """Record one attempt for *client_key* and return True if within quota."""
        now = time.monotonic()
        cutoff = now - self._window_seconds
        bucket = self._hits[client_key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self._max_requests:
            return False
        bucket.append(now)
        return True


class ACPTransportAuth:
    """Bearer + CSPRNG HttpOnly cookie + CSRF authenticator for the ACP bridge."""

    def __init__(
        self,
        *,
        bearer_token: str | None = None,
        token_env_var: str = DEFAULT_TOKEN_ENV_VAR,
        session_ttl_seconds: int = 3600,
        tls_enabled: bool = False,
        allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS,
    ) -> None:
        env_token = os.environ.get(token_env_var, "").strip() if token_env_var else ""
        if bearer_token is not None and bearer_token.strip():
            self._bearer_token = bearer_token.strip()
            self.ephemeral = False
        elif env_token:
            self._bearer_token = env_token
            self.ephemeral = False
        else:
            # TODO(security): set AGENT_FORGE_ACP_TOKEN in multi-instance setups
            _logger.warning("Generating ephemeral ACP transport bearer token (instance-isolated).")
            self._bearer_token = secrets.token_urlsafe(32)
            self.ephemeral = True
        self._session_ttl = max(60, session_ttl_seconds)
        self._tls_enabled = tls_enabled
        # TODO(security): enable tls_enabled=True behind HTTPS outside local loopback dev
        self._cookie_name = "__Host-af_acp_session" if tls_enabled else "af_acp_session"
        self._allowed_origins = frozenset(
            origin.rstrip("/") for origin in allowed_origins if origin.strip() and origin != "*"
        )
        self._sessions: dict[str, _SessionRecord] = {}

    @property
    def bearer_token(self) -> str:
        """Active bridge bearer token (never logged or returned over HTTP)."""
        return self._bearer_token

    @property
    def cookie_name(self) -> str:
        """Configured session cookie name."""
        return self._cookie_name

    @property
    def session_ttl_seconds(self) -> int:
        """Session lifetime in seconds."""
        return self._session_ttl

    @property
    def tls_enabled(self) -> bool:
        """Whether Secure cookies and __Host- prefix are active."""
        return self._tls_enabled

    def is_origin_allowed(self, origin: str | None) -> bool:
        """Validate an HTTP or WebSocket ``Origin`` header when provided."""
        if origin is None or not origin.strip():
            return True
        return origin.rstrip("/") in self._allowed_origins

    def has_forbidden_query_token(self, query_keys: Any) -> bool:
        """Reject any credentials passed via URL query parameters."""
        return any(str(k).lower() in _FORBIDDEN_QUERY_TOKEN_KEYS for k in query_keys)

    def verify_bearer(self, auth_header: str | None) -> bool:
        """Constant-time validation for an ``Authorization: Bearer ...`` header."""
        if not auth_header:
            return False
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return False
        return hmac.compare_digest(token.strip(), self._bearer_token)

    def create_session(self) -> tuple[str, str]:
        """Issue a CSPRNG session token and bound CSRF token."""
        self._prune_expired()
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self._sessions[sid] = _SessionRecord(
            expires_at=time.monotonic() + self._session_ttl,
            csrf_token=csrf,
        )
        return sid, csrf

    def get_session(self, sid: str | None) -> _SessionRecord | None:
        """Return valid active session record, pruning if expired."""
        if not sid:
            return None
        rec = self._sessions.get(sid)
        if rec is None:
            return None
        if time.monotonic() >= rec.expires_at:
            self._sessions.pop(sid, None)
            return None
        return rec

    def revoke_session(self, sid: str | None) -> None:
        """Invalidate an active browser session immediately."""
        if sid:
            self._sessions.pop(sid, None)

    def _prune_expired(self) -> None:
        now = time.monotonic()
        expired = [sid for sid, rec in self._sessions.items() if now >= rec.expires_at]
        for sid in expired:
            self._sessions.pop(sid, None)

    def authenticate_http(
        self,
        request: Request,
        *,
        require_csrf: bool = False,
    ) -> str:
        """Authenticate an HTTP request via Bearer token or session cookie (+ CSRF on POST).

        Returns ``"bearer"`` or ``"cookie"``. Raises HTTPException(401/403) on failure.
        """
        if self.has_forbidden_query_token(request.query_params.keys()):
            raise HTTPException(status_code=401, detail="Credentials in URL query are forbidden")
        if self.verify_bearer(request.headers.get("authorization")):
            return "bearer"
        sid = request.cookies.get(self._cookie_name)
        rec = self.get_session(sid)
        if rec is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if require_csrf:
            csrf_hdr = request.headers.get("x-csrf-token", "")
            if not csrf_hdr or not hmac.compare_digest(csrf_hdr, rec.csrf_token):
                raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")
        return "cookie"

    def authenticate_websocket(self, ws: WebSocket) -> int | None:
        """Validate Origin and credentials for a WebSocket upgrade.

        Returns ``None`` on success or a close code (``4401`` / ``4403``) on failure.
        """
        if self.has_forbidden_query_token(ws.query_params.keys()):
            return 4401
        if not self.is_origin_allowed(ws.headers.get("origin")):
            return 4403
        if self.verify_bearer(ws.headers.get("authorization")):
            return None
        sid = ws.cookies.get(self._cookie_name)
        if self.get_session(sid) is not None:
            return None
        return 4401


class _WebSocketLineReader:
    """Async newline reader backed by a FastAPI WebSocket text channel."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._buffered: deque[bytes] = deque()

    async def readline(self) -> bytes:
        if self._buffered:
            return self._buffered.popleft()
        try:
            text = await self._ws.receive_text()
        except WebSocketDisconnect:
            return b""
        lines = text.splitlines()
        if not lines:
            return b"\n"
        for line in lines:
            self._buffered.append((line + "\n").encode("utf-8"))
        return self._buffered.popleft()


class _WebSocketLineWriter:
    """Async line writer buffering bytes written by ACPServer and sending over WebSocket."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._buffer = bytearray()

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)

    async def drain(self) -> None:
        while b"\n" in self._buffer:
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                await self._ws.send_text(text)

    def close(self) -> None:
        self._buffer.clear()


def _attach_security_middleware(
    app: FastAPI,
    auth: ACPTransportAuth,
    limiter: SlidingWindowRateLimiter,
) -> None:
    @app.middleware("http")
    async def _security_and_rate_limit(request: Request, call_next: Any) -> Any:
        origin = request.headers.get("origin")
        if origin and not auth.is_origin_allowed(origin):
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        client_key = request.client.host if request.client else "local"
        if not limiter.allow(client_key):
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CSP_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        if origin and auth.is_origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, X-CSRF-Token"
            )
            response.headers["Vary"] = "Origin"
        return response


def _register_auth_and_health_routes(app: FastAPI, auth: ACPTransportAuth) -> None:
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/auth/session")
    async def create_auth_session(request: Request) -> JSONResponse:
        if auth.has_forbidden_query_token(request.query_params.keys()):
            raise HTTPException(status_code=401, detail="Credentials in URL query are forbidden")
        if not auth.verify_bearer(request.headers.get("authorization")):
            raise HTTPException(status_code=401, detail="Bearer token required")
        sid, csrf = auth.create_session()
        resp = JSONResponse(
            {"csrfToken": csrf, "expiresIn": auth.session_ttl_seconds},
            status_code=200,
        )
        resp.set_cookie(
            key=auth.cookie_name,
            value=sid,
            max_age=auth.session_ttl_seconds,
            httponly=True,
            secure=auth.tls_enabled,
            samesite="strict",
            path="/",
        )
        return resp

    @app.post("/auth/logout")
    async def logout_auth_session(request: Request) -> JSONResponse:
        auth.authenticate_http(request, require_csrf=True)
        auth.revoke_session(request.cookies.get(auth.cookie_name))
        resp = JSONResponse({"status": "logged_out"}, status_code=200)
        resp.delete_cookie(key=auth.cookie_name, path="/")
        return resp


def _register_workspace_file_routes(
    app: FastAPI,
    auth: ACPTransportAuth,
    workspace: WorkspaceBridge,
) -> None:
    @app.get("/workspace/tree")
    async def workspace_tree(
        request: Request,
        path: str = "",
        max_depth: int = 6,
    ) -> dict[str, Any]:
        auth.authenticate_http(request)
        try:
            entries = await asyncio.to_thread(workspace.list_tree, path, max_depth=max_depth)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"entries": entries}

    @app.get("/workspace/file")
    async def workspace_read_file(request: Request, path: str) -> dict[str, Any]:
        auth.authenticate_http(request)
        try:
            return await asyncio.to_thread(workspace.read_file, path)
        except PayloadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/workspace/file")
    async def workspace_write_file(request: Request, body: WriteFileBody) -> dict[str, Any]:
        auth.authenticate_http(request, require_csrf=True)
        try:
            return await asyncio.to_thread(workspace.write_file, body.path, body.content)
        except PayloadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _register_workspace_git_routes(
    app: FastAPI,
    auth: ACPTransportAuth,
    workspace: WorkspaceBridge,
) -> None:
    @app.get("/workspace/git/status")
    async def workspace_git_status(request: Request) -> dict[str, Any]:
        auth.authenticate_http(request)
        return await workspace.git_status()

    @app.get("/workspace/git/diff")
    async def workspace_git_diff(
        request: Request,
        path: str | None = None,
        staged: bool = False,
    ) -> dict[str, Any]:
        auth.authenticate_http(request)
        try:
            return await workspace.git_diff(path, staged=staged)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _reject_ws_if_needed(
    ws: WebSocket,
    auth: ACPTransportAuth,
    limiter: SlidingWindowRateLimiter,
) -> bool:
    code = auth.authenticate_websocket(ws)
    if code is not None:
        await ws.close(code=code)
        return True
    client_key = ws.client.host if ws.client else "local"
    if not limiter.allow(client_key):
        await ws.close(code=4429)
        return True
    return False


async def _pump_terminal_output(ws: WebSocket, term: WorkspaceTerminalSession) -> None:
    while True:
        chunk = await term.read_chunk(timeout=0.05)
        if chunk is None:
            exit_code = term.poll_exit_code()
            await ws.send_json({"type": "exit", "code": 0 if exit_code is None else exit_code})
            return
        if chunk:
            await ws.send_json({"type": "output", "data": chunk.decode("utf-8", errors="replace")})


async def _pump_terminal_input(ws: WebSocket, term: WorkspaceTerminalSession) -> None:
    while True:
        payload = await ws.receive_json()
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "input":
            data = str(payload.get("data", ""))
            if data:
                term.write(data.encode("utf-8"))
        elif kind == "resize":
            cols = int(payload.get("cols", 80))
            rows = int(payload.get("rows", 24))
            term.resize(cols=cols, rows=rows)


async def _run_terminal_bridge(ws: WebSocket, root: Path) -> None:
    term = WorkspaceTerminalSession(root)
    term.start()
    out_task = asyncio.create_task(_pump_terminal_output(ws, term))
    in_task = asyncio.create_task(_pump_terminal_input(ws, term))
    try:
        done, pending = await asyncio.wait(
            {out_task, in_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                task.result()
    finally:
        term.close()


async def _run_watch_loop(
    ws: WebSocket,
    workspace: WorkspaceBridge,
    interval_seconds: float,
    initial_snapshot: dict[str, tuple[int, int]],
) -> None:
    prev = initial_snapshot

    async def _recv_until_close() -> None:
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                await ws.receive_text()

    recv_task = asyncio.create_task(_recv_until_close())
    try:
        while not recv_task.done():
            await asyncio.sleep(interval_seconds)
            if recv_task.done():
                break
            curr = await asyncio.to_thread(workspace.snapshot_files)
            diff = workspace.diff_snapshots(prev, curr)
            prev = curr
            if diff["added"] or diff["modified"] or diff["deleted"]:
                await ws.send_json({"type": "workspace.changed", **diff})
    finally:
        recv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recv_task


def _register_websocket_routes(
    app: FastAPI,
    auth: ACPTransportAuth,
    limiter: SlidingWindowRateLimiter,
    workspace: WorkspaceBridge,
    server_factory: Callable[[], ACPServer],
    watch_interval_seconds: float,
) -> None:
    @app.websocket("/ws/acp")
    async def acp_websocket(ws: WebSocket) -> None:
        if await _reject_ws_if_needed(ws, auth, limiter):
            return
        await ws.accept()
        reader = _WebSocketLineReader(ws)
        writer = _WebSocketLineWriter(ws)
        acp_server = server_factory()
        try:
            await acp_server.serve(reader, writer)
        finally:
            writer.close()

    @app.websocket("/ws/workspace/watch")
    async def workspace_watch_ws(ws: WebSocket) -> None:
        if await _reject_ws_if_needed(ws, auth, limiter):
            return
        initial_snapshot = await asyncio.to_thread(workspace.snapshot_files)
        await ws.accept()
        await _run_watch_loop(ws, workspace, watch_interval_seconds, initial_snapshot)

    @app.websocket("/ws/terminal")
    async def workspace_terminal_ws(ws: WebSocket) -> None:
        if await _reject_ws_if_needed(ws, auth, limiter):
            return
        await ws.accept()
        await _run_terminal_bridge(ws, workspace.root)


def create_acp_ws_app(
    *,
    workspace_root: str | Path,
    server_factory: Callable[[], ACPServer],
    bearer_token: str | None = None,
    token_env_var: str = DEFAULT_TOKEN_ENV_VAR,
    allowed_origins: tuple[str, ...] | list[str] | None = None,
    rate_limit_requests: int = 120,
    rate_limit_window_seconds: float = 60.0,
    session_ttl_seconds: int = 3600,
    tls_enabled: bool = False,
    watch_interval_seconds: float = 0.25,
) -> FastAPI:
    """Create the authenticated FastAPI app hosting ACP WebSocket and workspace routes."""
    origins = tuple(allowed_origins) if allowed_origins is not None else DEFAULT_ALLOWED_ORIGINS
    auth = ACPTransportAuth(
        bearer_token=bearer_token,
        token_env_var=token_env_var,
        session_ttl_seconds=session_ttl_seconds,
        tls_enabled=tls_enabled,
        allowed_origins=origins,
    )
    limiter = SlidingWindowRateLimiter(
        max_requests=rate_limit_requests,
        window_seconds=rate_limit_window_seconds,
    )
    workspace = WorkspaceBridge(workspace_root)

    app = FastAPI(
        title="Agent Forge ACP Bridge", version=__version__, docs_url=None, redoc_url=None
    )
    app.state.acp_auth = auth
    app.state.workspace = workspace

    _attach_security_middleware(app, auth, limiter)
    _register_auth_and_health_routes(app, auth)
    _register_workspace_file_routes(app, auth, workspace)
    _register_workspace_git_routes(app, auth, workspace)
    _register_websocket_routes(
        app, auth, limiter, workspace, server_factory, watch_interval_seconds
    )
    return app

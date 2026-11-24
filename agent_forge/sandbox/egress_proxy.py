"""Host-side domain-allowlisted HTTP/CONNECT egress proxy for sandboxed runs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hmac
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence
from urllib.parse import urlsplit

_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_STRIPPED_PROXY_HEADERS = frozenset(
    {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "upgrade"}
)
_MAX_HEAD_TIMEOUT_S = 10.0

ConnectorFn = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


def _is_ip_literal(value: str) -> bool:
    candidate = value.strip("[]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def _is_valid_dns_name(host: str) -> bool:
    if "." not in host or len(host) > 253:
        return False
    labels = host.split(".")
    if any(not _DNS_LABEL_RE.match(label) for label in labels):
        return False
    return not labels[-1].isdigit()


def validate_egress_domain(raw: str) -> str:
    """Validate and normalize an allowed egress domain or leading-wildcard rule."""
    cleaned = raw.strip().lower().rstrip(".")
    if not cleaned:
        msg = "Allowed egress domain cannot be empty"
        raise ValueError(msg)
    if any(ch.isspace() for ch in cleaned) or any(tok in cleaned for tok in ("://", "/", ":")):
        msg = f"Invalid egress domain '{raw}': pass a hostname without scheme, port, or path"
        raise ValueError(msg)
    if cleaned == "localhost" or _is_ip_literal(cleaned):
        msg = f"Refusing localhost or IP literal in egress allowlist: '{raw}'"
        raise ValueError(msg)

    if "*" in cleaned:
        if not cleaned.startswith("*."):
            msg = f"Wildcard egress domain must start with '*.': '{raw}'"
            raise ValueError(msg)
        suffix = cleaned[2:]
        if "*" in suffix or not _is_valid_dns_name(suffix):
            msg = f"Invalid wildcard egress domain: '{raw}'"
            raise ValueError(msg)
        return cleaned

    if not _is_valid_dns_name(cleaned):
        msg = f"Invalid egress domain: '{raw}'"
        raise ValueError(msg)
    return cleaned


def is_domain_allowed(host: str, allowed_domains: Iterable[str]) -> bool:
    """Return True if *host* matches an exact or ``*.domain`` rule in *allowed_domains*."""
    norm = host.strip().lower().rstrip(".")
    if not norm or norm == "localhost" or _is_ip_literal(norm):
        return False
    for raw_rule in allowed_domains:
        rule = raw_rule.strip().lower().rstrip(".")
        if norm == rule:
            return True
        if rule.startswith("*."):
            suffix = rule[1:]
            if norm.endswith(suffix) and len(norm) > len(suffix):
                return True
    return False


def _is_blocked_ip(ip_str: str) -> bool:
    addr = ipaddress.ip_address(ip_str)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


async def _safe_open_connection(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        msg = f"No addresses resolved for host '{host}'"
        raise OSError(msg)

    resolved_ips: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        if _is_blocked_ip(raw_ip):
            msg = f"Refusing non-public upstream IP '{raw_ip}' for '{host}'"
            raise PermissionError(msg)
        resolved_ips.append(raw_ip)

    return await asyncio.open_connection(resolved_ips[0], port)


def build_proxy_env_vars(proxy_url: str) -> dict[str, str]:
    """Return standard HTTP/HTTPS proxy environment variables for sandboxed processes."""
    return {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    }


def _parse_connect_target(target: str) -> tuple[str, int] | None:
    if ":" not in target or target.startswith("["):
        return None
    host, port_str = target.rsplit(":", 1)
    if not host or not port_str.isdigit():
        return None
    port = int(port_str)
    if not 1 <= port <= 65535:
        return None
    return host, port


def _parse_http_target(target: str, headers: dict[str, str]) -> tuple[str, int, str] | None:
    if target.startswith(("http://", "https://")):
        parsed = urlsplit(target)
        if not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        origin_path = parsed.path or "/"
        if parsed.query:
            origin_path = f"{origin_path}?{parsed.query}"
        return parsed.hostname, port, origin_path

    host_header = headers.get("host", "").strip()
    if not host_header or host_header.startswith("["):
        return None
    if ":" in host_header:
        host, port_str = host_header.rsplit(":", 1)
        if not port_str.isdigit():
            return None
        port = int(port_str)
    else:
        host, port = host_header, 80
    if not host or not 1 <= port <= 65535:
        return None
    return host, port, target or "/"


async def _write_http_error(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    body = f"{status} {reason}\n".encode()
    payload = (
        f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body
    writer.write(payload)
    await writer.drain()


async def _pipe_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (OSError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        if writer.can_write_eof():
            with contextlib.suppress(OSError, RuntimeError):
                writer.write_eof()


class EgressProxyServer:
    """Bounded host-side HTTP/CONNECT proxy enforcing an explicit domain allowlist."""

    def __init__(
        self,
        allowed_domains: Sequence[str],
        *,
        bind_host: str = "127.0.0.1",
        advertise_host: str | None = None,
        port: int = 0,
        auth_token: str | None = None,
        connector: ConnectorFn | None = None,
    ) -> None:
        if not allowed_domains:
            msg = "EgressProxyServer requires at least one allowed domain"
            raise ValueError(msg)
        self._allowed_domains = tuple(validate_egress_domain(item) for item in allowed_domains)
        self._bind_host = bind_host
        self._advertise_host = advertise_host or bind_host
        self._requested_port = port
        self._auth_token = auth_token
        self._connector: ConnectorFn = connector or _safe_open_connection
        self._server: asyncio.Server | None = None
        self._bound_port: int | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._allowed_requests: list[str] = []
        self._blocked_requests: list[str] = []

    @property
    def bound_port(self) -> int | None:
        """Bound TCP port once started, else ``None``."""
        return self._bound_port

    @property
    def proxy_url(self) -> str:
        """Proxy URL advertised to sandbox processes."""
        if self._bound_port is None:
            msg = "EgressProxyServer has not been started"
            raise RuntimeError(msg)
        if self._auth_token is not None:
            return f"http://af:{self._auth_token}@{self._advertise_host}:{self._bound_port}"
        return f"http://{self._advertise_host}:{self._bound_port}"

    @property
    def allowed_requests(self) -> list[str]:
        """Hostnames permitted by the proxy during its lifetime."""
        return list(self._allowed_requests)

    @property
    def blocked_requests(self) -> list[str]:
        """Hostnames rejected with HTTP 403 by the proxy during its lifetime."""
        return list(self._blocked_requests)

    async def start(self) -> str:
        """Start listening for proxy connections and return :attr:`proxy_url`."""
        if self._server is not None:
            return self.proxy_url
        self._server = await asyncio.start_server(
            self._handle_client_wrapper,
            host=self._bind_host,
            port=self._requested_port,
        )
        sockets = self._server.sockets
        if not sockets:
            await self.stop()
            msg = "Failed to bind egress proxy listening socket"
            raise OSError(msg)
        self._bound_port = int(sockets[0].getsockname()[1])
        return self.proxy_url

    async def stop(self) -> None:
        """Stop accepting connections and tear down active relay tasks."""
        server = self._server
        self._server = None
        self._bound_port = None
        if server is not None:
            server.close()
        tasks = list(self._client_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._client_tasks.clear()
        if server is not None:
            await server.wait_closed()

    async def _handle_client_wrapper(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            await self._handle_client(reader, writer)
        finally:
            if task is not None:
                self._client_tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError, RuntimeError):
                await writer.wait_closed()

    def _authorize_request(self, headers: dict[str, str]) -> bool:
        if self._auth_token is None:
            return True
        auth_header = headers.get("proxy-authorization", "").strip()
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "basic" or not token:
            return False
        try:
            decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        return hmac.compare_digest(decoded, f"af:{self._auth_token}")

    @staticmethod
    async def _read_http_head(
        reader: asyncio.StreamReader,
    ) -> tuple[str, str, str, list[tuple[str, str]], dict[str, str]] | None:
        try:
            raw_head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=_MAX_HEAD_TIMEOUT_S,
            )
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return None

        text = raw_head[:-4].decode("latin-1", errors="replace")
        lines = text.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            return None
        method, target, version = parts[0].upper(), parts[1], parts[2]
        raw_headers: list[tuple[str, str]] = []
        headers_map: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            raw_headers.append((name.strip(), value.strip()))
            headers_map[name.strip().lower()] = value.strip()
        return method, target, version, raw_headers, headers_map

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        parsed = await self._read_http_head(reader)
        if parsed is None:
            return
        method, target, version, raw_headers, headers_map = parsed

        if not self._authorize_request(headers_map):
            writer.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="agent-forge"\r\n'
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            return

        if method == "CONNECT":
            await self._handle_connect(target, reader, writer)
        elif method in _HTTP_METHODS:
            await self._handle_http_forward(
                method, target, version, raw_headers, headers_map, reader, writer
            )
        else:
            await _write_http_error(writer, 405, "Method Not Allowed")

    async def _dial_allowed(
        self, host: str, port: int, writer: asyncio.StreamWriter
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        if not is_domain_allowed(host, self._allowed_domains):
            self._blocked_requests.append(host)
            await _write_http_error(writer, 403, "Forbidden")
            return None
        try:
            conn = await self._connector(host, port)
        except PermissionError:
            self._blocked_requests.append(host)
            await _write_http_error(writer, 403, "Forbidden")
            return None
        except OSError:
            await _write_http_error(writer, 502, "Bad Gateway")
            return None
        self._allowed_requests.append(host)
        return conn

    async def _handle_connect(
        self,
        target: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        parsed_target = _parse_connect_target(target)
        if parsed_target is None:
            await _write_http_error(writer, 400, "Bad Request")
            return
        host, port = parsed_target
        upstream = await self._dial_allowed(host, port, writer)
        if upstream is None:
            return

        up_reader, up_writer = upstream
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                _pipe_stream(reader, up_writer),
                _pipe_stream(up_reader, writer),
                return_exceptions=True,
            )
        finally:
            up_writer.close()
            with contextlib.suppress(OSError, RuntimeError):
                await up_writer.wait_closed()

    async def _handle_http_forward(
        self,
        method: str,
        target: str,
        version: str,
        raw_headers: list[tuple[str, str]],
        headers_map: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        parsed_target = _parse_http_target(target, headers_map)
        if parsed_target is None:
            await _write_http_error(writer, 400, "Bad Request")
            return
        host, port, origin_path = parsed_target
        upstream = await self._dial_allowed(host, port, writer)
        if upstream is None:
            return

        up_reader, up_writer = upstream
        try:
            body = b""
            raw_len = headers_map.get("content-length")
            if raw_len and raw_len.isdigit() and int(raw_len) > 0:
                body = await reader.readexactly(int(raw_len))

            header_lines = [f"{method} {origin_path} {version}"]
            has_host = False
            for name, value in raw_headers:
                lower = name.lower()
                if lower in _STRIPPED_PROXY_HEADERS:
                    continue
                if lower == "host":
                    has_host = True
                header_lines.append(f"{name}: {value}")
            if not has_host:
                header_lines.append(
                    f"Host: {host}:{port}" if port not in (80, 443) else f"Host: {host}"
                )
            header_lines.append("Connection: close")
            head_bytes = ("\r\n".join(header_lines) + "\r\n\r\n").encode("latin-1")

            up_writer.write(head_bytes + body)
            await up_writer.drain()
            await _pipe_stream(up_reader, writer)
        except (asyncio.IncompleteReadError, OSError):
            pass
        finally:
            up_writer.close()
            with contextlib.suppress(OSError, RuntimeError):
                await up_writer.wait_closed()

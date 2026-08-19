"""Validation for request settings passed to PTB's HTTPX boundary."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlparse, urlunparse

HttpVersion = Literal["1.1", "2.0", "2"]
SocketOption = tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]


@dataclass(frozen=True)
class RequestConfiguration:
    """Validated settings accepted by :class:`telegram.request.HTTPXRequest`."""

    connection_pool_size: int = 1
    read_timeout: float | None = None
    write_timeout: float | None = None
    connect_timeout: float | None = None
    pool_timeout: float | None = None
    media_write_timeout: float | None = None
    http_version: HttpVersion = "1.1"
    socket_options: Collection[SocketOption] | None = None
    proxy: str | None = None
    httpx_kwargs: dict[str, object] | None = None


def request_kwargs(configuration: RequestConfiguration) -> dict[str, object]:
    """Build the auxiliary-bot keyword arguments from validated settings."""
    values: dict[str, object] = {
        "connection_pool_size": configuration.connection_pool_size,
        "http_version": configuration.http_version,
    }
    for name in ("read_timeout", "write_timeout", "connect_timeout", "pool_timeout", "media_write_timeout", "socket_options", "proxy", "httpx_kwargs"):
        value = getattr(configuration, name)
        if value is not None:
            values[name] = value
    return values


_REQUEST_OPTIONS = frozenset(
    {
        "connection_pool_size",
        "read_timeout",
        "write_timeout",
        "connect_timeout",
        "pool_timeout",
        "media_write_timeout",
        "http_version",
        "socket_options",
        "proxy",
        "httpx_kwargs",
        "proxy_url",
        "username",
        "password",
        "urllib3_proxy_kwargs",
    }
)


def _number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"request_kwargs.{name} must be a number or null")
    return float(value)


def _socket_options(value: object) -> Collection[SocketOption] | None:
    if value is None:
        return None
    if not isinstance(value, Collection) or isinstance(value, (str, bytes, bytearray, Mapping)):
        raise ValueError("request_kwargs.socket_options must be a collection of socket option tuples")
    options: list[SocketOption] = []
    for option in value:
        if not isinstance(option, (tuple, list)) or len(option) not in (3, 4):
            raise ValueError("request_kwargs.socket_options must contain three- or four-item tuples")
        level, option_name, option_value, *extra = option
        if type(level) is not int or type(option_name) is not int:
            raise ValueError("request_kwargs.socket_options must start with two integers")
        if len(extra) == 0 and type(option_value) is int:
            options.append((level, option_name, option_value))
        elif len(extra) == 0 and isinstance(option_value, (bytes, bytearray)):
            options.append((level, option_name, option_value))
        elif len(extra) == 1 and option_value is None and type(extra[0]) is int:
            options.append((level, option_name, None, extra[0]))
        else:
            raise ValueError("request_kwargs.socket_options contains an unsupported socket option")
    return options


def _proxy(data: Mapping[str, object]) -> str | None:
    proxy_auth = data.get("urllib3_proxy_kwargs", {})
    if proxy_auth is None:
        proxy_auth = {}
    if not isinstance(proxy_auth, Mapping):
        raise ValueError("request_kwargs.urllib3_proxy_kwargs must be a mapping")
    if unexpected := set(proxy_auth).difference(("username", "password")):
        raise ValueError(f"request_kwargs.urllib3_proxy_kwargs contains unsupported option(s): {', '.join(sorted(str(item) for item in unexpected))}")
    username = data.get("username", proxy_auth.get("username"))
    password = data.get("password", proxy_auth.get("password"))
    if username is not None and not isinstance(username, str):
        raise ValueError("request_kwargs.username must be a string or null")
    if password is not None and not isinstance(password, str):
        raise ValueError("request_kwargs.password must be a string or null")
    proxy = data.get("proxy", data.get("proxy_url"))
    if proxy is None:
        return None
    if not isinstance(proxy, str):
        raise ValueError("request_kwargs.proxy must be a string or null")
    parsed = urlparse(proxy)
    if username is not None and "@" not in parsed.netloc:
        netloc = quote(username, safe="")
        if password is not None:
            netloc += ":" + quote(password, safe="")
        netloc += f"@{parsed.hostname or ''}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    return proxy


def _http_version(value: object) -> HttpVersion:
    if value == "1.1":
        return "1.1"
    if value == "2.0":
        return "2.0"
    if value == "2":
        return "2"
    raise ValueError("request_kwargs.http_version must be one of: 1.1, 2.0, 2")


def parse_request_configuration(data: Mapping[str, object]) -> RequestConfiguration:
    """Validate supported request options and translate documented legacy proxy keys."""
    if unexpected := set(data).difference(_REQUEST_OPTIONS):
        raise ValueError(f"request_kwargs contains unsupported option(s): {', '.join(sorted(str(item) for item in unexpected))}")
    pool_size = data.get("connection_pool_size", 1)
    if type(pool_size) is not int or pool_size <= 0:
        raise ValueError("request_kwargs.connection_pool_size must be a positive integer")
    httpx_kwargs = data.get("httpx_kwargs")
    if httpx_kwargs is not None and not isinstance(httpx_kwargs, Mapping):
        raise ValueError("request_kwargs.httpx_kwargs must be a mapping or null")
    return RequestConfiguration(
        connection_pool_size=pool_size,
        read_timeout=_number(data.get("read_timeout"), "read_timeout"),
        write_timeout=_number(data.get("write_timeout"), "write_timeout"),
        connect_timeout=_number(data.get("connect_timeout"), "connect_timeout"),
        pool_timeout=_number(data.get("pool_timeout"), "pool_timeout"),
        media_write_timeout=_number(data.get("media_write_timeout"), "media_write_timeout"),
        http_version=_http_version(data.get("http_version", "1.1")),
        socket_options=_socket_options(data.get("socket_options")),
        proxy=_proxy(data),
        httpx_kwargs=dict(httpx_kwargs) if isinstance(httpx_kwargs, Mapping) else None,
    )

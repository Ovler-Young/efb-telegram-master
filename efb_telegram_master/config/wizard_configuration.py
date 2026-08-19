"""Schema validation and typed state for the setup wizard configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .request import RequestConfiguration, parse_request_configuration
from .runtime import RPCConfiguration


@dataclass
class WizardConfiguration:
    """Typed configuration values retained throughout the setup wizard."""

    token: str
    admins: list[int]
    flags: dict[str, object]
    request: RequestConfiguration | None = None
    rpc: RPCConfiguration | None = None
    additional_sections: dict[str, object] = field(default_factory=dict)

    @classmethod
    def defaults(cls) -> "WizardConfiguration":
        return cls(token="", admins=[], flags={})

    @classmethod
    def from_mapping(cls, data: object) -> "WizardConfiguration":
        if not isinstance(data, Mapping):
            raise ValueError("Config file must contain a mapping.")
        token = data.get("token")
        if not isinstance(token, str):
            raise ValueError("Telegram bot token must be a string")
        admins = data.get("admins", [])
        if type(admins) is int:
            admins = [admins]
        if isinstance(admins, str) and admins.isdigit():
            admins = [int(admins)]
        if not isinstance(admins, list):
            raise ValueError("Admins' user IDs must be a list of integers.")
        normalized_admins = [int(admin) if isinstance(admin, str) and admin.isdigit() else admin for admin in admins]
        if any(type(admin) is not int for admin in normalized_admins):
            raise ValueError("Admins' user IDs must be a list of integers.")
        flags = data.get("flags", {})
        if not isinstance(flags, Mapping):
            raise ValueError("flags must be a mapping.")
        request_kwargs = data.get("request_kwargs")
        if request_kwargs is not None and not isinstance(request_kwargs, Mapping):
            raise ValueError("request_kwargs must be a mapping.")
        rpc = data.get("rpc")
        return cls(
            token=token,
            admins=list(normalized_admins),
            flags=dict(flags),
            request=parse_request_configuration(request_kwargs) if isinstance(request_kwargs, Mapping) else None,
            rpc=RPCConfiguration.from_mapping(rpc) if rpc is not None else None,
            additional_sections={key: value for key, value in data.items() if key not in {"token", "admins", "flags", "request_kwargs", "rpc"}},
        )

    def to_mapping(self) -> dict[str, object]:
        data = {**self.additional_sections, "token": self.token, "admins": self.admins, "flags": self.flags}
        if self.request is not None:
            data["request_kwargs"] = _request_mapping(self.request)
        if self.rpc is not None:
            data["rpc"] = self.rpc.to_mapping()
        return data


def _request_mapping(configuration: RequestConfiguration) -> dict[str, object]:
    data: dict[str, object] = {"connection_pool_size": configuration.connection_pool_size, "http_version": configuration.http_version}
    for name in ("read_timeout", "write_timeout", "connect_timeout", "pool_timeout", "media_write_timeout", "socket_options", "proxy", "httpx_kwargs"):
        value = getattr(configuration, name)
        if value is not None:
            data[name] = list(value) if name == "socket_options" else value
    return data

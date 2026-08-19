"""Typed configuration loaded before the Telegram channel starts."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ehforwarderbot.types import ModuleID
from ruamel.yaml import YAML

from ..paths import get_config_path
from ..runtime.mtproto import MTProtoConfig
from .request import RequestConfiguration, parse_request_configuration

MAX_AUXILIARY_BOTS = 16
DEFAULT_MAX_PENDING = 1000


@dataclass(frozen=True)
class OutboundConfiguration:
    """Validated delivery queue settings."""

    max_pending: int


@dataclass(frozen=True)
class AuxiliaryBotConfiguration:
    """Credentials for one optional delivery bot."""

    token: str


@dataclass(frozen=True)
class RPCConfiguration:
    """Settings for the optional RPC interface."""

    server: str = "127.0.0.1"
    port: int = 8000
    additional_options: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: object) -> "RPCConfiguration":
        if not isinstance(data, Mapping):
            raise ValueError("rpc must be a mapping.")
        server = data.get("server", "127.0.0.1")
        port = data.get("port", 8000)
        if not isinstance(server, str):
            raise ValueError("rpc.server must be a string.")
        if type(port) is not int:
            raise ValueError("rpc.port must be an integer.")
        return cls(server=server, port=port, additional_options={key: value for key, value in data.items() if key not in {"server", "port"}})

    def to_mapping(self) -> dict[str, object]:
        return {**self.additional_options, "server": self.server, "port": self.port}


@dataclass(frozen=True)
class RuntimeConfiguration:
    """All validated settings consumed by the running Telegram channel."""

    token: str
    admins: tuple[int, ...]
    flags: dict[str, object]
    topic_group: object | None
    database: dict[str, object]
    rpc: RPCConfiguration | None
    mtproto: MTProtoConfig
    outbound: OutboundConfiguration
    auxiliary_bots: tuple[AuxiliaryBotConfiguration, ...]
    request: RequestConfiguration
    metrics: object | None
    webhook: Mapping[str, object] | None

    @classmethod
    def from_mapping(cls, data: object) -> "RuntimeConfiguration":
        if not isinstance(data, Mapping):
            raise ValueError("Config file must contain a mapping.")
        token = data.get("token")
        if not isinstance(token, str):
            raise ValueError("Telegram bot token must be a string")
        admins = _admins(data.get("admins"))
        flags = _mapping_section(data, "flags", {})
        database = _mapping_section(data, "database", {})
        mtproto = MTProtoConfig.from_mapping(data.get("mtproto"))
        if mtproto.enabled and not token:
            raise ValueError("MTProto requires a non-empty Telegram bot token")
        outbound = _outbound(data.get("outbound", {}))
        auxiliary_bots = _auxiliary_bots(data.get("auxiliary_bots", []), token)
        request = _runtime_request(data.get("request_kwargs"))
        rpc_data = data.get("rpc")
        rpc = RPCConfiguration.from_mapping(rpc_data) if rpc_data is not None else None
        webhook_data = data.get("webhook")
        if webhook_data is not None and not isinstance(webhook_data, Mapping):
            raise ValueError("webhook must be a mapping")
        return cls(
            token=token,
            admins=admins,
            flags=flags,
            topic_group=data.get("topic_group"),
            database=database,
            rpc=rpc,
            mtproto=mtproto,
            outbound=outbound,
            auxiliary_bots=auxiliary_bots,
            request=request,
            metrics=data.get("metrics"),
            webhook=dict(webhook_data) if isinstance(webhook_data, Mapping) else None,
        )


def load_channel_config(channel_id: ModuleID, translate: Callable[[str], str]) -> RuntimeConfiguration:
    """Load YAML once and return the validated runtime configuration."""
    config_path = get_config_path(channel_id)
    if not config_path.exists():
        raise FileNotFoundError(translate("Config File does not exist. ({path})").format(path=config_path))
    with config_path.open() as config_file:
        try:
            return RuntimeConfiguration.from_mapping(YAML().load(config_file))
        except ValueError as error:
            raise ValueError(translate(str(error))) from error


def _admins(value: object) -> tuple[int, ...]:
    if type(value) is int:
        value = [value]
    if isinstance(value, str) and value.isdigit():
        value = [int(value)]
    if not isinstance(value, list) or not value:
        raise ValueError("Admins' user IDs must be a list of one number or more.")
    admins = tuple(int(admin) if isinstance(admin, str) and admin.isdigit() else admin for admin in value)
    if not all(type(admin) is int for admin in admins):
        invalid = next(admin for admin in admins if type(admin) is not int)
        raise ValueError(f"Admin ID is expected to be an int, but {invalid} is found.")
    return admins


def _mapping_section(data: Mapping[object, object], name: str, default: dict[str, object]) -> dict[str, object]:
    value = data.get(name, default)
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping.")
    return dict(value)


def _outbound(value: object) -> OutboundConfiguration:
    if not isinstance(value, Mapping):
        raise ValueError("outbound must be a mapping.")
    max_pending = value.get("max_pending", DEFAULT_MAX_PENDING)
    if type(max_pending) is not int or max_pending <= 0:
        raise ValueError("outbound.max_pending must be a positive integer.")
    return OutboundConfiguration(max_pending=max_pending)


def _auxiliary_bots(value: object, main_token: str) -> tuple[AuxiliaryBotConfiguration, ...]:
    if not isinstance(value, list):
        raise ValueError("auxiliary_bots must be a list.")
    if len(value) > MAX_AUXILIARY_BOTS:
        raise ValueError(f"auxiliary_bots must contain at most {MAX_AUXILIARY_BOTS} entries.")
    seen_tokens = {main_token}
    bots: list[AuxiliaryBotConfiguration] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("token"), str):
            raise ValueError(f'auxiliary_bots[{index}] must have a "token" string.')
        token = entry["token"]
        if token in seen_tokens:
            raise ValueError(f"Duplicate token found in auxiliary_bots[{index}].")
        seen_tokens.add(token)
        bots.append(AuxiliaryBotConfiguration(token=token))
    return tuple(bots)


def _runtime_request(value: object) -> RequestConfiguration:
    configured = {} if value is None else value
    if not isinstance(configured, Mapping):
        raise ValueError("request_kwargs must be a mapping")
    multiplier = 2.0
    try:
        configured_multiplier = float(os.getenv("ETM_HTTPX_POOL_MULTIPLIER", multiplier))
        multiplier = configured_multiplier if configured_multiplier > 0 else multiplier
    except ValueError:
        pass
    request = {"read_timeout": 15.0, "connection_pool_size": max(1, int(round(8 * multiplier)))}
    request.update(configured)
    return parse_request_configuration(request)

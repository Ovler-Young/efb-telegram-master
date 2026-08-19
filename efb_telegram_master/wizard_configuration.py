"""Schema validation for configuration loaded by the setup wizard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .request_configuration import parse_request_configuration


@dataclass(frozen=True)
class WizardConfiguration:
    """Typed configuration values the wizard reads before interactive updates."""

    token: str
    admins: list[int]
    flags: dict[str, object]
    values: dict[str, object]

    @classmethod
    def from_mapping(cls, data: object) -> "WizardConfiguration":
        if not isinstance(data, Mapping):
            raise ValueError("Config file must contain a mapping.")
        values = dict(data)
        token = values.get("token")
        if not isinstance(token, str):
            raise ValueError("Telegram bot token must be a string")
        admins = values.get("admins", [])
        if type(admins) is int:
            admins = [admins]
        if isinstance(admins, str) and admins.isdigit():
            admins = [int(admins)]
        if not isinstance(admins, list):
            raise ValueError("Admins' user IDs must be a list of integers.")
        admins = [int(admin) if isinstance(admin, str) and admin.isdigit() else admin for admin in admins]
        if any(type(admin) is not int for admin in admins):
            raise ValueError("Admins' user IDs must be a list of integers.")
        flags = values.get("flags", {})
        if not isinstance(flags, Mapping):
            raise ValueError("flags must be a mapping.")
        request_kwargs = values.get("request_kwargs")
        if request_kwargs is not None:
            if not isinstance(request_kwargs, Mapping):
                raise ValueError("request_kwargs must be a mapping.")
            parse_request_configuration(request_kwargs)
            values["request_kwargs"] = dict(request_kwargs)
        values["token"] = token
        values["admins"] = list(admins)
        values["flags"] = dict(flags)
        return cls(token=token, admins=list(admins), flags=dict(flags), values=values)

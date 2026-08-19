from collections.abc import Mapping
from gettext import translation
from typing import Optional

import cjkwrap
from ehforwarderbot import coordinator
from ehforwarderbot.types import ModuleID
from ruamel.yaml import YAML
from telegram import Bot
from telegram.request import HTTPXRequest

from . import TelegramChannel
from .paths import LOCALE_DIR, get_config_path
from .telegram_runtime import build_request
from .utils import normalize_request_kwargs
from .wizard_configuration import WizardConfiguration

translator = translation("efb_telegram_master", str(LOCALE_DIR), fallback=True)
_ = translator.gettext


def print_wrapped(text):
    for paragraph in text.split("\n"):
        print(*cjkwrap.wrap(paragraph), sep="\n")


class DataModel:
    data: dict

    def __init__(self, profile: str, instance_id: str):
        self.request: Optional[HTTPXRequest] = None
        self.building_default = False
        coordinator.profile = profile
        self.profile = profile
        self.instance_id = instance_id
        self.channel_id = TelegramChannel.channel_id
        if instance_id:
            self.channel_id = ModuleID(self.channel_id + "#" + instance_id)
        self.config_path = get_config_path(self.channel_id)
        self.yaml = YAML()
        if not self.config_path.exists():
            self.build_default_config()
        else:
            configuration = WizardConfiguration.from_mapping(self.yaml.load(self.config_path.open()))
            self.data = configuration.values
            request_kwargs = self.data.get("request_kwargs")
            if isinstance(request_kwargs, Mapping) and request_kwargs:
                self.request = build_request(normalize_request_kwargs(request_kwargs))

    def build_default_config(self):
        self.data = {"token": "", "admins": [], "flags": {}}
        self.building_default = True

    def save(self):
        if self.building_default:
            with self.config_path.open("w") as f:
                f.write(
                    _(
                        # TRANSLATORS: This part of text must be formatted in a monospaced font and no line shall exceed the width of a 70-cell-wide terminal.
                        "# ======================================\n"
                        "# EFB Telegram Master Configuration file\n"
                        "# ======================================\n"
                        "#\n"
                        "# This file configures how EFB Telegram Master Channel (ETM) works, and\n"
                        "# Who it belongs to.\n"
                        "#\n"
                        "# Required items\n"
                        "# --------------\n"
                        "#\n"
                        "# [Bot Token]\n"
                        "# The bot token obtained from @BotFather, in the format of numbers-colon-\n"
                        "# alphanumerals.\n"
                    )
                )
                f.write("\n")
                self.yaml.dump({"token": self.data["token"]}, f)
                f.write("\n")
                f.write(
                    _(
                        # TRANSLATORS: This part of text must be formatted in a monospaced font.and no line shall exceed the width of a 70-cell-wide terminal.
                        "# [List of Admin User IDs]\n# ETM will only process messages and commands from users\n# listed below.  This ID can be obtained from various ways \n# on Telegram.\n"
                    )
                )
                f.write("\n")
                self.yaml.dump({"admins": self.data["admins"]}, f)
                f.write("\n")
                f.write(
                    _(
                        # TRANSLATORS: This part of text mst be formatted in a monospaced font.and no line shall exceed the width of a 70-cell-wide terminal.
                        "# Optional items\n"
                        "# --------------\n"
                        "#\n"
                        "# [Experimental Flags]\n"
                        "# This section can be used to toggle experimental functionality.\n"
                        "# These features may be changed or removed at any time.\n"
                        "# Refer to the project documentation for details.\n"
                        "#\n"
                        "# https://etm.1a23.studio\n"
                    )
                )
                f.write("\n")
                self.yaml.dump({"flags": self.data["flags"]}, f)
                f.write("\n")
                f.write(
                    _(
                        # TRANSLATORS: This part of text mst be formatted in a monospaced font.and no line shall exceed the width of a 70-cell-wide terminal.
                        "# [Network configurations]\n# Timeout tweaks, Proxy, etc.\n# Refer to the project documentation for details.\n#\n# https://etm.1a23.studio\n"
                    )
                )
                f.write("\n")
                if self.data.get("request_kwargs"):
                    self.yaml.dump({"request_kwargs": self.data["request_kwargs"]}, f)
                else:
                    f.write(
                        "# request_kwargs:\n"
                        "#     # HTTP Proxy\n"
                        "#     proxy_url: http://127.0.0.1:80/\n"
                        "#     # username: admin\n"
                        "#     # password: password\n"
                        "#\n"
                        "#     # SOCKS5 proxy (Additional installations required)\n"
                        "#     # proxy_url: socks5://127.0.0.1:1080/\n"
                        "#     # urllib3_proxy_kwargs:\n"
                        "#     #     username: admin\n"
                        "#     #     password: password\n"
                    )
                f.write("\n")
                f.write(
                    _(
                        # TRANSLATORS: This part of text mst be formatted in a monospaced font.and no line shall exceed the width of a 70-cell-wide terminal.
                        "# [RPC interface]\n"
                        "# Enable RPC interface of ETM where you can use scripts to manage data stored\n"
                        "# in the ETM message database or make queries.\n"
                        "# Refer to the project documentation for details.\n"
                        "#\n"
                        "# https://etm.1a23.studio\n"
                    )
                )
                f.write("\n")
                if self.data.get("rpc"):
                    self.yaml.dump({"rpc": self.data["rpc"]}, f)
                else:
                    f.write("# rpc:\n#     server: 127.0.0.1\n#     port: 8000\n")
                f.write("\n")
            with self.config_path.open() as f:
                self.data = self.yaml.load(f)
            self.building_default = False
        else:
            with self.config_path.open("w") as f:
                self.yaml.dump(self.data, f)


def build_bot(data: DataModel, token: Optional[str] = None) -> Bot:
    bot_kwargs: dict = {"token": token or data.data["token"]}
    if data.request is not None:
        bot_kwargs["request"] = data.request
    return Bot(**bot_kwargs)

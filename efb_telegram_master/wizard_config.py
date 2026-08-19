from gettext import translation
from typing import Optional

import cjkwrap
from ehforwarderbot import coordinator
from ehforwarderbot.types import ModuleID
from ruamel.yaml import YAML
from telegram import Bot

from . import TelegramChannel
from .paths import LOCALE_DIR, get_config_path
from .wizard_configuration import WizardConfiguration

translator = translation("efb_telegram_master", str(LOCALE_DIR), fallback=True)
_ = translator.gettext


def print_wrapped(text):
    for paragraph in text.split("\n"):
        print(*cjkwrap.wrap(paragraph), sep="\n")


class DataModel:
    def __init__(self, profile: str, instance_id: str):
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
            self.configuration = WizardConfiguration.from_mapping(self.yaml.load(self.config_path.open()))

    def build_default_config(self):
        self.configuration = WizardConfiguration.defaults()
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
                self.yaml.dump({"token": self.configuration.token}, f)
                f.write("\n")
                f.write(
                    _(
                        # TRANSLATORS: This part of text must be formatted in a monospaced font.and no line shall exceed the width of a 70-cell-wide terminal.
                        "# [List of Admin User IDs]\n# ETM will only process messages and commands from users\n# listed below.  This ID can be obtained from various ways \n# on Telegram.\n"
                    )
                )
                f.write("\n")
                self.yaml.dump({"admins": self.configuration.admins}, f)
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
                self.yaml.dump({"flags": self.configuration.flags}, f)
                f.write("\n")
                f.write(
                    _(
                        # TRANSLATORS: This part of text mst be formatted in a monospaced font.and no line shall exceed the width of a 70-cell-wide terminal.
                        "# [Network configurations]\n# Timeout tweaks, Proxy, etc.\n# Refer to the project documentation for details.\n#\n# https://etm.1a23.studio\n"
                    )
                )
                f.write("\n")
                if self.configuration.request is not None:
                    self.yaml.dump({"request_kwargs": self.configuration.to_mapping()["request_kwargs"]}, f)
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
                if self.configuration.rpc is not None:
                    self.yaml.dump({"rpc": self.configuration.rpc.to_mapping()}, f)
                else:
                    f.write("# rpc:\n#     server: 127.0.0.1\n#     port: 8000\n")
                f.write("\n")
            with self.config_path.open() as f:
                self.configuration = WizardConfiguration.from_mapping(self.yaml.load(f))
            self.building_default = False
        else:
            with self.config_path.open("w") as f:
                self.yaml.dump(self.configuration.to_mapping(), f)


def build_bot(configuration: WizardConfiguration, token: Optional[str] = None) -> Bot:
    bot_token = token or configuration.token
    if configuration.request is None:
        return Bot(token=bot_token)
    from .transport.telegram_runtime import build_request

    return Bot(token=bot_token, request=build_request(configuration.request))

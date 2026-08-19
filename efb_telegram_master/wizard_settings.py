from dataclasses import replace

from bullet import Bullet, Numbers, YesNo

from .request_configuration import RequestConfiguration
from .wizard_config import _, print_wrapped
from .wizard_configuration import RPCConfiguration

flags_settings = {
    "chats_per_page": (10, "int", None, _("Number of chats shown in when choosing for /chat and /link command. An overly large value may lead to malfunction of such commands.")),
    "multiple_slave_chats": (
        True,
        "bool",
        None,
        _("Link more than one remote chat to one Telegram group. Send and reply as you do with an unlinked chat. Disable to link remote chats and Telegram group one-to-one."),
    ),
    "network_error_prompt_interval": (100, "int", None, _("Notify the user about network error every n errors received. Set to 0 to disable it.")),
    "prevent_message_removal": (True, "bool", None, _("When a slave channel requires to remove a message, EFB will ignore the request if this value is true.")),
    "auto_locale": (True, "bool", None, _("Detect the locale from admins’ messages automatically. Locale defined in environment variables will be used otherwise.")),
    "retry_on_error": (
        False,
        "bool",
        None,
        _(
            "Retry infinitely when an error occurred while sending request to "
            "Telegram Bot API. Note that this may lead to repetitive message "
            "delivery, as the respond of Telegram Bot API is not reliable, and "
            "may not reflect the actual result."
        ),
    ),
    "send_image_as_file": (False, "bool", None, _("Send all image messages as files, in order to prevent Telegram’s image compression in an aggressive way.")),
    "message_muted_on_slave": (
        "normal",
        "choices",
        ["normal", "silent", "mute"],
        _(
            "Behavior when a message received is muted on slave channel platform.\n\n- normal: send to Telegram as normal message\n- silent: send to Telegram as normal message, but without notification sound\n- mute: do not send to Telegram"
        ),
    ),
    "your_message_on_slave": (
        "silent",
        "choices",
        ["normal", "silent", "mute"],
        _(
            "Behavior when a message received is from you on slave channel platform. This overrides settings from message_muted_on_slave.\n\n- normal: send to Telegram as normal message\n- silent: send to Telegram as normal message, but without notification sound\n- mute: do not send to Telegram"
        ),
    ),
    "animated_stickers": (False, "bool", None, _('Enable experimental support to animated stickers. Note: you might need to install binary dependency "libcairo" to enable this feature.')),
    "send_to_last_chat": (
        "warn",
        "choices",
        ["enabled", "warn", "disabled"],
        _(
            "Enable quick reply in non-linked chats.\n\n- enabled: Enable this feature without warning.\n- warn: Enable this feature and issue warnings every time when you switch a recipient with quick reply.\n- disabled: Disable this feature."
        ),
    ),
    "default_media_prompt": (
        "emoji",
        "choices",
        ["emoji", "text", "disabled"],
        _(
            "Placeholder text when the a picture/video/file message has no caption.\n\n- emoji: Use emoji like 🖼️, 🎥, and 📄.\n- text: Use text like “Sent a picture/video/file”.\n- disabled: Use empty placeholders."
        ),
    ),
}


def setup_experimental_flags(data):
    print()
    widget = YesNo(prompt=_("Do you want to config experimental features? "), prompt_prefix="[yN] ", default="n")
    if not widget.launch():
        return
    for key, value in flags_settings.items():
        default, cat, params, desc = value
        if data.configuration.flags.get(key) is not None:
            default = data.configuration.flags.get(key)
        if cat == "bool":
            prompt_prefix = "[Yn] " if default else "[yN] "
            print()
            print(key)
            print_wrapped(desc)
            ans = YesNo(prompt=f"{key}? ", default="y" if default else "n", prompt_prefix=prompt_prefix).launch()
            data.configuration.flags[key] = ans
        elif cat == "int":
            print()
            print(key)
            print_wrapped(desc)
            ans = Numbers(prompt=f"{key} [{default}]? ", type=int).launch(default=default)
            data.configuration.flags[key] = ans
        elif cat == "choices":
            try:
                assert isinstance(params, list)
                default = params.index(default)
            except ValueError:
                default = 0
            print()
            print(key)
            print_wrapped(desc)
            ans = Bullet(prompt=f"{key}?", choices=params).launch(default=default)
            data.configuration.flags[key] = ans


def setup_network_configurations(data):
    print()
    proceed = YesNo(prompt=_("Do you want to adjust network configurations? (connection timeout) "), default="n", prompt_prefix="[yN] ").launch()
    if not proceed:
        return
    print_wrapped(_("For meanings and significances of the following values, please consult the module documentations."))
    print()
    print("https://etm.1a23.studio/")
    print()
    if YesNo(prompt=_("Do you want to change timeout settings? "), prompt_prefix="[yN] ", default="n").launch():
        request = data.configuration.request or RequestConfiguration()
        data.configuration.request = replace(
            request,
            read_timeout=float(Numbers(prompt=_("read_timeout (in seconds): ")).launch()),
            connect_timeout=float(Numbers(prompt=_("connect_timeout (in seconds): ")).launch()),
        )


def setup_rpc(data):
    print()
    print_wrapped(_("To learn about what RPC is and what it does, please visit the module documentations."))
    print()
    print("https://etm.1a23.studio/")
    print()
    proceed = YesNo(prompt=_("Do you want to enable RPC interface? "), prompt_prefix="[yN] ", default="n").launch()
    if not proceed:
        return
    server = "127.0.0.1"
    port = 8000
    existing_rpc = data.configuration.rpc
    if existing_rpc is not None:
        server = existing_rpc.server
        port = existing_rpc.port
    server = input(_("RPC server: ") + f"[{server}] ") or server
    port = int(input(_("Proxy port: ") + f"[{port}] ") or port)
    data.configuration.rpc = RPCConfiguration(server=server, port=port, additional_options=existing_rpc.additional_options if existing_rpc is not None else {})

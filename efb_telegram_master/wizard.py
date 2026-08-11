import shutil

from PIL import Image, WebPImagePlugin

from .wizard_config import DataModel, _, print_wrapped
from .wizard_settings import setup_experimental_flags, setup_network_configurations, setup_rpc
from .wizard_steps import setup_admins, setup_proxy, setup_telegram_bot, setup_telegram_bot_commands_list


def prerequisites_check():
    print(_("Checking ffmpeg installation..."), end="", flush=True)
    if shutil.which("ffmpeg") is None:
        print(_("FAILED"))
        print_wrapped(_("ffmpeg is not found in current $PATH."))
        exit(1)
    print(_("OK"))
    print(_("Checking libmagic installation..."), end="", flush=True)
    try:
        import magic

        assert magic
    except ImportError:
        print(_("FAILED"))
        print_wrapped(_("libmagic is not found in your system."))
        exit(1)
    print(_("OK"))
    print(_("Checking libwebp installation..."), end="", flush=True)
    Image.init()
    if "WEBP" not in Image.ID or not getattr(WebPImagePlugin, "SUPPORTED", None):
        print(_("FAILED"))
        print_wrapped(_("libwebp plugin is not detected by Pillow."))
        exit(1)
    print(_("OK"))
    print()


def wizard(profile, instance_id):
    data = DataModel(profile, instance_id)
    prerequisites_check()
    print_wrapped(
        _(
            "================================\n"
            "EFB Telegram Master Setup Wizard\n"
            "================================\n"
            "\n"
            "This wizard will guide you to setup your EFB Telegram Master channel "
            "(ETM). This would be really fast and simple."
        )
    )
    print()
    setup_proxy(data)
    setup_telegram_bot(data)
    setup_telegram_bot_commands_list(data)
    setup_admins(data)
    setup_experimental_flags(data)
    setup_network_configurations(data)
    setup_rpc(data)
    print(_("Saving configurations..."), end="", flush=True)
    data.save()
    print(_("OK"))
    print()
    print_wrapped(_("Congratulations! You have finished the setup wizard for EFB Telegram Master Channel."))

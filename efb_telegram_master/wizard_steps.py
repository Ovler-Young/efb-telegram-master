import asyncio
import threading
from getpass import getpass

from bullet import Bullet, YesNo
from telegram import Bot
from telegram.error import TelegramError

from .telegram_runtime import build_request
from .utils import normalize_request_kwargs
from .wizard_config import DataModel, _, build_bot, print_wrapped


async def _id_bot_loop(bot: Bot, stop_event: threading.Event):
    offset = None
    while not stop_event.is_set():
        updates = await bot.get_updates(offset=offset, timeout=5)
        for update in updates:
            offset = update.update_id + 1
            if update.effective_message and update.effective_user:
                await bot.send_message(
                    chat_id=update.effective_message.chat_id,
                    text=_("Your Telegram user ID is {id}.").format(id=update.effective_user.id),
                )


def start_id_bot(data: DataModel):
    stop_event = threading.Event()

    def runner():
        asyncio.run(_id_bot_loop(build_bot(data), stop_event))

    thread = threading.Thread(target=runner, daemon=True, name="ETMWizardIDBot")
    thread.start()
    return stop_event, thread


def input_bot_token(data: DataModel, default=None):
    prompt = _("Your Telegram Bot token: ")
    if default:
        prompt += f"[{default}] "
    while True:
        ans = input(prompt)
        if not ans:
            if default:
                return default
            else:
                print(_("Bot token is required. Please try again."))
                continue
        else:
            try:
                asyncio.run(build_bot(data, ans).get_me())
            except TelegramError as e:
                print_wrapped(str(e))
                print()
                print(_("Please try again."))
                continue
            return ans


def setup_proxy(data):
    if YesNo(prompt=_("Do you want to run ETM behind a proxy? "), prompt_prefix="[yN] ", default="n").launch():
        if data.data.get("request_kwargs") is None:
            data.data["request_kwargs"] = {}
        proxy_type = Bullet(prompt=_("Select proxy type"), choices=["http", "socks5"]).launch()
        host = input(_("Proxy host (domain/IP): "))
        port = input(_("Proxy port: "))
        username = None
        password = None
        if YesNo(prompt=_("Does it require authentication? "), prompt_prefix="[yN] ", default="n").launch():
            username = input(_("Username: "))
            password = getpass(_("Password: "))
        if proxy_type == "http":
            data.data["request_kwargs"]["proxy_url"] = f"http://{host}:{port}/"
            if username is not None and password is not None:
                data.data["request_kwargs"]["username"] = username
                data.data["request_kwargs"]["password"] = password
        elif proxy_type == "socks5":
            try:
                import socks

                assert socks
            except ModuleNotFoundError as error:
                print_wrapped(_("You have not installed required extra package to use SOCKS5 proxy, please install with the following command:"))
                print()
                print("pip install 'python-telegram-bot[socks]'")
                print()
                raise error
            protocol = input(_("Protocol [socks5]: ")) or "socks5"
            data.data["request_kwargs"]["proxy_url"] = f"{protocol}://{host}:{port}"
            if username is not None and password is not None:
                data.data["request_kwargs"]["urllib3_proxy_kwargs"] = {"username": username, "password": password}
        data.request = build_request(normalize_request_kwargs(data.data["request_kwargs"]))


def setup_telegram_bot(data):
    print_wrapped(_("1. Set up your Telegram Bot\n---------------------------\nETM requires you to have a Telegram bot ready with you to start with."))
    print()
    if data.data["token"]:
        # Config has token ready.
        # Assuming user doesn't need help creating one
        data.data["token"] = input_bot_token(data, data.data["token"])
    else:
        # No config is ready.
        # prompt to guide user to create one.

        prompt_yes = _("Yes, please tell me how to make one.")
        prompt_no = _("No, I have already made one according to the docs.")

        choices = Bullet(prompt=_("Do you need help creating a bot?"), choices=[prompt_no, prompt_yes])
        answer = choices.launch()

        if answer == prompt_yes:
            print_wrapped(
                _("Follow this guide to create your first ETM Telegram Bot.\n\n>>> Step 1: Search @BotFather on Telegram, or follow the link below. You should be able to see a bot named “BotFather”.")
            )
            print("    https://t.me/BotFather")
            print()
            input(_("Press ENTER/RETURN to continue..."))
            print()
            print_wrapped(
                _(
                    ">>> Step 2: Send /newbot to BotFather to create a new bot. "
                    "Follow its prompts to give it a name and a username. "
                    "Note that its username must end with “bot”.\n"
                    "\n"
                    "After setting its username, you should receive a long line "
                    "of code called “token”. Keep it with you securely, we will "
                    "need that later on."
                )
            )
            print()
            input(_("Press ENTER/RETURN to continue..."))
            print()
            print_wrapped(
                _(
                    ">>> Step 3: Get the bot ready for ETM.\n"
                    "Send /setjoingroups to BotFather, choose the bot you "
                    "just created, then choose “Enable”. This will allow your bot "
                    "to join groups.\n"
                    "\n"
                    "Send /setprivacy to BotFather, choose the bot you just "
                    "created, then choose “Disable”. This will allow your bot to "
                    "process all messages in groups it joined, not just commands."
                )
            )
            print()
            input(_("Press ENTER/RETURN to continue..."))
        print()
        data.data["token"] = input_bot_token(data)


def setup_telegram_bot_commands_list(data):
    prompt_yes = _("Yes, please update.")
    prompt_no = _("No, I want to keep the old commands list.")

    choices = Bullet(prompt=_("Do you want to update the list of commands of your bot?"), choices=[prompt_yes, prompt_no])
    answer = choices.launch()

    if answer == prompt_yes:
        print(_("Updating commands list..."), end="", flush=True)
        asyncio.run(
            build_bot(data).set_my_commands(
                [
                    ("help", _("Show commands list.")),
                    ("link", _("Link a remote chat to a group.")),
                    ("unlink_all", _("Unlink all remote chats from a group.")),
                    ("info", _("Display information of the current Telegram chat.")),
                    ("chat", _("Generate a chat head.")),
                    ("extra", _("Access additional features from Slave Channels.")),
                    ("update_info", _("Update info of linked Telegram group.")),
                    ("react", _("Send a reaction to a message, or show a list of reactors.")),
                    ("rm", _("Remove a message from its remote chat.")),
                ]
            )
        )

        print(_("OK"))
        print()
        input(_("Press ENTER/RETURN to continue..."))


def input_admin_ids(default=None):
    prompt = _("List of Admin User IDs, separated with comma: ")
    if default:
        default_prompt = ",".join(map(str, default))
        prompt += f"[{default_prompt}] "
    while True:
        ans = input(prompt)
        if not ans:
            if default:
                return default
            else:
                print(_("Admin IDs are required. Please try again."))
                continue
        else:
            try:
                values = [int(i.strip()) for i in ans.split(",")]
            except ValueError:
                print_wrapped(_("{input} is not a valid input. Please try again.").format(input=ans))
                continue
            return values


def setup_admins(data):
    print()
    print_wrapped(
        _("2. Set up Bot administrators\n----------------------------\nTo protect your data privacy and security, you need to provide a list of users who can interact with this Telegram Bot.")
    )
    print()
    if data.data["admins"]:
        data.data["admins"] = input_admin_ids(default=data.data["admins"])
    else:
        prompt_yes = _("Yes, I want to know how to get my ID.")
        prompt_no = _("No, I already know my ID.")

        choices = Bullet(prompt=_("Do you need help getting your ID?"), choices=[prompt_no, prompt_yes])
        answer = choices.launch()

        if answer == prompt_yes:
            print(_("Starting ID bot..."), end="", flush=True)
            stop_event, id_bot_thread = start_id_bot(data)

            print(_("OK"))
            print()

            print_wrapped(
                _("Now, send any message to the bot you just created. You should be able to get a numerical ID. That is your Telegram user ID. Enter that below to set yourself as an admin.")
            )
            print()
            data.data["admins"] = input_admin_ids(default=data.data["admins"])
            print()
            print(_("Stopping ID bot..."), end="", flush=True)
            stop_event.set()
            id_bot_thread.join(timeout=10.0)
            print(_("OK"))
        else:
            data.data["admins"] = input_admin_ids(default=data.data["admins"])

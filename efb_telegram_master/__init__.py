# coding=utf-8

import gettext
import html
import logging
import mimetypes
import os
import shlex
import time
from gettext import NullTranslations, translation
from typing import Callable, List, Optional
from xmlrpc.server import SimpleXMLRPCServer

import ehforwarderbot  # lgtm [py/import-and-import-from]
import telegram  # lgtm [py/import-and-import-from]
import telegram.constants
import telegram.error
from ehforwarderbot import Channel, coordinator
from ehforwarderbot.channel import MasterChannel
from ehforwarderbot.chat import Chat
from ehforwarderbot.constants import MsgType
from ehforwarderbot.exceptions import EFBChatNotFound, EFBException, EFBMessageReactionNotPossible, EFBOperationNotSupported
from ehforwarderbot.message import Message as EFBMessage
from ehforwarderbot.status import ReactToMessage, Status
from ehforwarderbot.types import ChatID, InstanceID, MessageID, ModuleID, ReactionName
from language_tags import tags
from PIL import Image, WebPImagePlugin
from ruamel.yaml import YAML
from telegram import Message, Update
from telegram.constants import ChatType
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler

from . import utils as etm_utils
from .__version__ import __version__
from .bot_manager import TelegramBotManager
from .chat_binding import ChatBindingManager
from .chat_destination_cache import ChatDestinationCache
from .chat_object_cache import ChatObjectCacheManager
from .commands import CommandsManager
from .db import DatabaseManager
from .master_message import MasterMessageProcessor
from .message import ETMMsg
from .mtproto import MTProtoClient, MTProtoConfig, MTProtoRetryableError
from .paths import LOCALE_DIR, get_config_path
from .ptb_compat import Filters, get_forwarded_chat, sync_reply_html, sync_reply_text
from .rpc_utils import RPCUtilities
from .slave_message import SlaveMessageProcessor
from .telegram_runtime import TelegramPollingRuntime
from .utils import EFBChannelChatIDStr, ExperimentalFlagsManager, TelegramChatID, TelegramMessageID


class TelegramChannel(MasterChannel):
    """
    EFB Channel - Telegram (Master)
    Based on python-telegram-bot, Telegram Bot API

    Author: Eana Hufwe <https://github.com/blueset>

    Configuration file example:
        .. code-block:: yaml

            token: "12345678:1a2b3c4d5e6g7h8i9j"
            admins:
            - 102938475
            - 91827364
            flags:
                join_msg_threshold_secs: 10
                multiple_slave_chats: false
    """

    # Meta Info
    channel_name = "Telegram Master"
    channel_emoji = "✈"
    channel_id = ModuleID("blueset.telegram")
    supported_message_types = {MsgType.Text, MsgType.File, MsgType.Voice, MsgType.Image, MsgType.Link, MsgType.Location, MsgType.Sticker, MsgType.Video, MsgType.Animation, MsgType.Status}
    __version__ = __version__

    # Data
    _stop_polling = False
    timeout_count = 0
    last_poll_confliction_time = 0.0
    CONFLICTION_TIMEOUT = 60  # seconds since last confliction warnings received

    # Constants
    config: dict

    # Translator
    translator: NullTranslations = translation("efb_telegram_master", os.fspath(LOCALE_DIR), fallback=True)
    locale: Optional[str] = None

    # RPC server
    rpc_server: Optional[SimpleXMLRPCServer] = None

    def __init__(self, instance_id: Optional[InstanceID] = None):
        """
        Initialization.
        """
        super().__init__(instance_id)  # type: ignore[arg-type]  # upstream Channel.__init__ accepts None but isn't annotated as Optional

        # Check PIL support for WebP
        Image.init()
        if "WEBP" not in Image.ID or not getattr(WebPImagePlugin, "SUPPORTED", None):
            raise EFBException(self._("WebP support of Pillow is required.\nPlease refer to Pillow Documentation for instructions.\nhttps://pillow.readthedocs.io/"))

        # Suppress debug logs from dependencies
        # logging.getLogger('requests').setLevel(logging.CRITICAL)
        # logging.getLogger('urllib3').setLevel(logging.CRITICAL)
        # logging.getLogger('telegram.bot').setLevel(logging.CRITICAL)
        # logging.getLogger('telegram.vendor.ptb_urllib3.urllib3.connectionpool').setLevel(logging.CRITICAL)

        # Set up logger
        self.logger: logging.Logger = logging.getLogger(__name__)

        # Load configs
        self.load_config()

        # Load predefined MIME types
        mimetypes.init(files=["mimetypes"])

        # Initialize managers
        self.flag: ExperimentalFlagsManager = ExperimentalFlagsManager(self)
        self.db: DatabaseManager = DatabaseManager(self)
        self.mtproto = MTProtoClient(self.mtproto_config, self.config["token"], self.db._base_path)
        self.chat_manager: ChatObjectCacheManager = ChatObjectCacheManager(self)
        self.chat_dest_cache: ChatDestinationCache = ChatDestinationCache(self.flag("send_to_last_chat"))
        self.bot_manager: TelegramBotManager = TelegramBotManager(self)
        self.telegram_runtime = self.bot_manager.telegram_runtime
        self.commands: CommandsManager = CommandsManager(self)
        self.chat_binding: ChatBindingManager = ChatBindingManager(self)
        self.slave_messages: SlaveMessageProcessor = SlaveMessageProcessor(self)
        self.topic_group: Optional[TelegramChatID] = TelegramChatID(self.flag("topic_group"))

        if not self.flag("auto_locale"):
            self.translator = translation("efb_telegram_master", os.fspath(LOCALE_DIR), fallback=True)

        # Basic message handlers
        non_edit_filter = Filters.update.message | Filters.update.channel_post
        self.telegram_runtime.application.add_handler(CommandHandler("start", self.telegram_runtime.as_async_callback(self.start), filters=non_edit_filter))
        self.telegram_runtime.application.add_handler(CommandHandler("help", self.telegram_runtime.as_async_callback(self.help), filters=non_edit_filter))
        self.telegram_runtime.application.add_handler(CommandHandler("info", self.telegram_runtime.as_async_callback(self.info), filters=non_edit_filter))
        self.telegram_runtime.application.add_handler(CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.void_callback_handler), pattern="void"))
        self.telegram_runtime.application.add_handler(CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.bot_manager.api.session_expired)))
        self.telegram_runtime.application.add_handler(CommandHandler("react", self.telegram_runtime.as_async_callback(self.react), filters=non_edit_filter))
        self.telegram_runtime.application.add_handler(CommandHandler("sync_msglog", self.telegram_runtime.as_async_callback(self.sync_msglog), filters=non_edit_filter))

        # Register master message handlers after commands to prevent commands
        # commands to be delivered as messages
        self.master_messages: MasterMessageProcessor = MasterMessageProcessor(self)

        self.telegram_runtime.application.add_error_handler(self.telegram_runtime.as_async_callback(self.error))

        self.rpc_utilities = RPCUtilities(self)

    @property
    def _(self) -> Callable[[str], str]:
        return self.translator.gettext

    @property
    def ngettext(self) -> Callable[[str, str, int], str]:
        return self.translator.ngettext

    def load_config(self):
        """
        Load configuration from path specified by the framework.

        Configuration file is in YAML format.
        """
        config_path = get_config_path(self.channel_id)
        if not config_path.exists():
            raise FileNotFoundError(self._("Config File does not exist. ({path})").format(path=config_path))
        with config_path.open() as f:
            data = YAML().load(f)

            # Verify configuration
            if not isinstance(data.get("token", None), str):
                raise ValueError(self._("Telegram bot token must be a string"))
            self.mtproto_config = MTProtoConfig.from_mapping(data.get("mtproto"))
            if self.mtproto_config.enabled and not data["token"]:
                raise ValueError(self._("MTProto requires a non-empty Telegram bot token"))
            if isinstance(data.get("admins", None), int):
                data["admins"] = [data["admins"]]
            if isinstance(data.get("admins", None), str) and data["admins"].isdigit():
                data["admins"] = [int(data["admins"])]
            if not isinstance(data.get("admins", None), list) or not data["admins"]:
                raise ValueError(self._("Admins' user IDs must be a list of one number or more."))
            for i in range(len(data["admins"])):
                if isinstance(data["admins"][i], str) and data["admins"][i].isdigit():
                    data["admins"][i] = int(data["admins"][i])
                if not isinstance(data["admins"][i], int):
                    raise ValueError(self._("Admin ID is expected to be an int, but {data} is found.").format(data=data["admins"][i]))

            # Validate auxiliary_bots config
            aux_bots = data.get("auxiliary_bots", [])
            if aux_bots:
                if not isinstance(aux_bots, list):
                    raise ValueError(self._("auxiliary_bots must be a list."))
                main_token = data["token"]
                seen_tokens = {main_token}
                for idx, entry in enumerate(aux_bots):
                    if not isinstance(entry, dict) or not isinstance(entry.get("token"), str):
                        raise ValueError(self._('auxiliary_bots[{idx}] must have a "token" string.').format(idx=idx))
                    if entry["token"] in seen_tokens:
                        raise ValueError(self._("Duplicate token found in auxiliary_bots[{idx}].").format(idx=idx))
                    seen_tokens.add(entry["token"])

            self.config = data.copy()

    def info(self, update: Update, context: CallbackContext):
        """
        Show info of the current telegram conversation.
        Triggered by `/info`.
        """
        assert isinstance(update, Update)
        assert isinstance(update.effective_message, Message)
        if update.effective_message.chat.type != ChatType.PRIVATE:  # Group message
            if update.effective_chat and update.effective_chat.is_forum:
                msg = self.info_topic(update)
            else:
                msg = self.info_group(update)
        elif (forwarded_chat := get_forwarded_chat(update.effective_message)) and forwarded_chat.type == ChatType.CHANNEL:  # Forwarded channel command.
            msg = self.info_channel(update)
        else:  # Talking to the bot.
            msg = self.info_general()

        if len(msg) > 4095:
            for x in range(0, len(msg), 4095):
                sync_reply_text(self.bot_manager.api, update.effective_message, msg[x : x + 4095])
        else:
            sync_reply_text(self.bot_manager.api, update.effective_message, msg)

    def info_topic(self, update: Update):
        """Generate string for chat linking info of a topic."""
        assert isinstance(update, Update)
        assert isinstance(update.effective_message, Message)

        topic_links = self.db.get_topic_slaves(topic_chat_id=TelegramChatID(update.effective_message.chat_id))
        thread_id = update.effective_message.message_thread_id
        chat_ids: List[EFBChannelChatIDStr] = []
        if thread_id:
            if topic_links:
                for dest, topic_id in topic_links:
                    if topic_id == thread_id:
                        chat_ids = [dest]
                        break
            if not chat_ids:
                return "This chat is not managed by this bot"
        else:
            if topic_links:
                chat_ids = [c for c, t in topic_links]

        msg = self._("The topic {topic_name} ({topic_id}) is linked to:").format(topic_name=update.effective_message.chat.title, topic_id=update.effective_message.chat_id)
        msg += self.build_link_chats_info_str(chat_ids)
        return msg

    def info_general(self):
        """Generate string for information of the current running EFB instance."""
        if self.instance_id:
            if coordinator.profile != "default":
                msg = self._("This is EFB Telegram Master Channel {version}, running on profile “{profile}”, instance “{instance}”, on EFB {fw_version}.")
            else:  # Default profile
                msg = self._("This is EFB Telegram Master Channel {version}, running on default profile, instance “{instance}”, on EFB {fw_version}.")
        else:  # Default instance
            if coordinator.profile != "default":
                msg = self._("This is EFB Telegram Master Channel {version}, running on profile “{profile}”, default instance, on EFB {fw_version}.")
            else:  # Default profile
                msg = self._("This is EFB Telegram Master Channel {version}, running on default profile and instance, on EFB {fw_version}.")
        msg = msg.format(version=self.__version__, fw_version=ehforwarderbot.__version__, profile=coordinator.profile, instance=self.instance_id)
        msg += "\n" + self.ngettext("{count} slave channel activated:", "{count} slave channels activated:", len(coordinator.slaves)).format(count=len(coordinator.slaves))
        for i in coordinator.slaves:
            msg += "\n- %s %s (%s, %s)" % (coordinator.slaves[i].channel_emoji, coordinator.slaves[i].channel_name, i, coordinator.slaves[i].__version__)
        if coordinator.middlewares:
            msg += self.ngettext("\n\n{count} middleware activated:", "\n\n{count} middlewares activated:", len(coordinator.middlewares)).format(count=len(coordinator.middlewares))
            for i in coordinator.middlewares:
                msg += "\n- %s (%s, %s)" % (i.middleware_name, i.middleware_id, i.__version__)
        return msg

    def info_channel(self, update):
        """Generate string for chat linking info of a channel."""
        chat = get_forwarded_chat(update.effective_message)
        assert chat is not None
        links = self.db.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, chat.id))
        if links:  # Linked chat
            # TRANSLATORS: ‘channel’ here refers to a Telegram channel.
            msg = self._("The channel {group_name} ({group_id}) is linked to:").format(group_name=chat.title, group_id=chat.id)
            msg += self.build_link_chats_info_str(links)
        else:
            # TRANSLATORS: ‘channel’ here means an EFB channel.
            msg = self._("The channel {group_name} ({group_id}) is not linked to any remote chat. To link one, use /link.").format(group_name=chat.title, group_id=chat.id)
        return msg

    def info_group(self, update):
        """Generate string for chat linking info of a group."""
        links = self.db.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, update.message.chat_id))
        if links:  # Linked chat
            msg = self._("The group {group_name} ({group_id}) is linked to:").format(group_name=update.message.chat.title, group_id=update.message.chat_id)
            msg += self.build_link_chats_info_str(links)
        else:
            msg = self._("The group {group_name} ({group_id}) is not linked to any remote chat. To link one, use /link.").format(group_name=update.message.chat.title, group_id=update.message.chat_id)
        return msg

    def build_link_chats_info_str(self, links: List[EFBChannelChatIDStr]) -> str:
        """Build a string indicating all linked chats in argument.

        Returns:
            String that starts with a line break.
        """
        msg = ""
        for i in links:
            channel_id, chat_id, _ = etm_utils.chat_id_str_to_id(i)
            chat_object = self.chat_manager.get_chat(channel_id, chat_id)
            if chat_object:
                msg += "\n- %s (%s:%s)" % (chat_object.full_name, channel_id, chat_id)
            else:
                try:
                    module = coordinator.get_module_by_id(channel_id)
                    if isinstance(module, Channel):
                        channel_name = f"{module.channel_emoji} {module.channel_name}"
                    else:  # module is Middleware
                        channel_name = module.middleware_name
                    msg += self._("\n- {channel_name}: Unknown chat ({channel_id}:{chat_id})").format(channel_name=channel_name, channel_id=channel_id, chat_id=chat_id)
                except NameError:
                    # TRANSLATORS: ‘channel’ here means an EFB channel.
                    msg += self._("\n- Unknown channel {channel_id}: ({chat_id})").format(channel_id=channel_id, chat_id=chat_id)
        return msg

    def update_locale(self, update: Update, context: CallbackContext):
        if not self.flag("auto_locale"):
            return
        if not update.effective_user or not update.effective_user.language_code:
            return

        language_code = update.effective_user.language_code
        if language_code == self.locale:
            return

        self.locale = language_code
        tag = tags.tag(language_code)
        if tag.language:
            locale = tag.language.format
            if tag.region:
                locale += "_" + tag.region.format
        else:
            locale = language_code.replace("-", "_")
        self.logger.info(
            "Telegram locale updated",
            extra={"event": "telegram_channel.locale_updated", "locale": locale},
        )
        self.translator = gettext.translation(
            "efb_telegram_master",
            os.fspath(LOCALE_DIR),
            languages=[locale, "C"],
            fallback=True,
        )

    def start(self, update: Update, context: CallbackContext):
        """
        Process bot command `/start`.
        """
        assert isinstance(update, Update)
        assert isinstance(update.effective_message, telegram.Message)
        assert isinstance(update.effective_chat, telegram.Chat)
        command_args = self._resolve_command_args(update.effective_message.text, context.args)
        if command_args:  # Group binding command
            forwarded_chat = get_forwarded_chat(update.effective_message)
            if (update.effective_message.chat.type != ChatType.PRIVATE and update.effective_chat.id != self.topic_group) or (
                forwarded_chat and forwarded_chat.type == ChatType.CHANNEL and forwarded_chat.id != self.topic_group
            ):
                self.chat_binding.link_chat(update, command_args)
            else:
                self.bot_manager.api.send_message(update.effective_chat.id, self._("You cannot link remote chats to here. Please try again."))
        else:
            txt = self._("This is EFB Telegram Master Channel.\n\nTo learn more, please visit https://etm.1a23.studio .")
            self.bot_manager.api.send_message(update.effective_chat.id, txt)

    @staticmethod
    def _resolve_command_args(message_text: Optional[str], parsed_args: Optional[List[str]]) -> List[str]:
        args = list(parsed_args or [])
        if not message_text:
            return args
        try:
            raw_args = shlex.split(message_text)[1:]
        except ValueError:
            raw_args = message_text.split()[1:]
        if len(raw_args) > len(args):
            return raw_args
        return args

    def react(self, update: Update, context: CallbackContext):
        """React to a message."""
        assert isinstance(update, Update)
        assert isinstance(update.effective_message, Message)
        message: Message = update.effective_message

        reaction = None
        args = message.text and message.text.split(" ", 1)
        if args and len(args) > 1:
            reaction = ReactionName(args[1])

        if not message.reply_to_message:
            sync_reply_html(
                self.bot_manager.api,
                message,
                self._("Reply to a message with this command and an emoji to send a reaction. Ex.: <code>/react 👍</code>.\nSend <code>/react -</code> to remove your reaction from a message."),
            )
            return

        target: Message = message.reply_to_message
        msg_log = self.db.get_msg_log(master_msg_id=etm_utils.message_id_to_str(chat_id=TelegramChatID(target.chat_id), message_id=TelegramMessageID(target.message_id)))
        if msg_log is None:
            sync_reply_text(self.bot_manager.api, message, self._("The message you replied to is not recorded in ETM database. You cannot react to this message."))
            return
        if msg_log.provenance == "mtproto_ingested":
            sync_reply_text(self.bot_manager.api, message, self._("This recovered message cannot be reacted to from its remote chat."))
            return

        if not reaction:
            msg_log_obj: ETMMsg = msg_log.build_etm_msg(self.chat_manager)
            reactors = msg_log_obj.reactions
            if not reactors:
                sync_reply_html(
                    self.bot_manager.api, message, self._("This message has no reactions yet. Reply to a message with this command and an emoji to send a reaction. Ex.: <code>/react 👍</code>.")
                )
                return
            else:
                text = ""
                for key, values in reactors.items():
                    if not values:
                        continue
                    text += f"{key}:\n"
                    for j in values:
                        text += f"    {j.display_name}\n"
                text = text.strip()
                sync_reply_text(self.bot_manager.api, message, text)
                return

        message_id = msg_log.slave_message_id
        channel_id, chat_uid, _ = etm_utils.chat_id_str_to_id(EFBChannelChatIDStr(msg_log.slave_origin_uid))

        if channel_id not in coordinator.slaves:
            sync_reply_text(self.bot_manager.api, message, self._("The slave channel involved in this message ({}) is not available. You cannot react to this message.").format(channel_id))
            return

        channel = coordinator.slaves[channel_id]

        if channel.suggested_reactions is None:
            sync_reply_text(self.bot_manager.api, message, self._("The channel involved in this message ({}) does not accept reactions. You cannot react to this message.").format(channel_id))
            return

        try:
            chat_obj = channel.get_chat(chat_uid)
        except EFBChatNotFound:
            sync_reply_text(self.bot_manager.api, message, self._("The chat involved in this message ({}) is not found. You cannot react to this message.").format(chat_uid))
            return

        if reaction == ReactionName("-"):
            reaction = None

        try:
            coordinator.send_status(ReactToMessage(chat=chat_obj, msg_id=message_id, reaction=reaction))
        except EFBOperationNotSupported:
            sync_reply_text(self.bot_manager.api, message, self._("You cannot react anything to this message."))
            return
        except EFBMessageReactionNotPossible:
            prompt = self._("{} is not accepted as a reaction to this message.").format(reaction)
            if channel.suggested_reactions:
                # TRANSLATORS: {} is a list of names of possible reactions, separated with comma.
                prompt += "\n" + self._("You may want to try: {}").format(", ".join(channel.suggested_reactions[:10]))
            sync_reply_text(self.bot_manager.api, message, prompt)
            return

    def sync_msglog(self, update: Update, context: CallbackContext):
        """Schedule durable MsgLog ingestion for the current bound forum group."""
        assert isinstance(update, Update)
        if update.effective_message is None:
            return
        if update.effective_user is None or update.effective_user.id not in self.config["admins"]:
            sync_reply_text(self.bot_manager.api, update.effective_message, self._("This command is for ETM admins only."))
            return
        if update.effective_chat is None or not update.effective_chat.is_forum:
            sync_reply_text(self.bot_manager.api, update.effective_message, self._("This command must be used in a bound forum group."))
            return
        group_id = TelegramChatID(update.effective_chat.id)
        if not self.db.get_topic_slaves(group_id):
            sync_reply_text(self.bot_manager.api, update.effective_message, self._("This forum group has no bound topics."))
            return
        state = self.chat_binding.schedule_msglog_ingestion(int(group_id))
        sync_reply_text(self.bot_manager.api, update.effective_message, self._("MsgLog sync {state} for this group.").format(state=state))

    def help(self, update: Update, context: CallbackContext):
        assert isinstance(update, Update)
        assert isinstance(update.message, Message)
        txt = self._(
            "EFB Telegram Master Channel\n"
            "/start <token> [true|false]\n"
            "/link\n"
            "    Link a remote chat to an empty Telegram group.\n"
            "    Followed by a regular expression to filter results.\n"
            "/chat\n"
            "    Generate a chat head to start a conversation.\n"
            "    Followed by a regular expression to filter results.\n"
            "/extra\n"
            "    List all additional features from slave channels.\n"
            "/unlink_all\n"
            "    Unlink all remote chats in this chat.\n"
            "/info\n"
            "    Show information of the current Telegram chat.\n"
            "/react [emoji]\n"
            "    React to a message with an emoji, or show a list of members reacted.\n"
            "/update_info\n"
            "    Update info of linked Telegram group.\n"
            "    Only works in singly linked group where the bot is an admin.\n"
            "/init_topics\n"
            "/sync_msglog\n"
            "/rm\n"
            "    Remove the quoted message from its remote chat.\n"
            "/help\n"
            "    Print this command list."
        )
        sync_reply_text(self.bot_manager.api, update.message, txt)

    async def _telegram_runtime_started(self, runtime: TelegramPollingRuntime) -> None:
        for auxiliary in self.bot_manager.api.bot_pool.bots if self.bot_manager.api.bot_pool else []:
            auxiliary.bind_runtime(runtime.async_runtime)
        if not self.mtproto.enabled:
            return
        try:
            await self.mtproto.connect()
        except (ConnectionError, TimeoutError, OSError, MTProtoRetryableError) as error:
            self.logger.warning(
                "MTProto startup is unavailable; MsgLog ingestion remains pending (%s).",
                type(error).__name__,
                extra={"event": "telegram_channel.mtproto_start_failed", "error_type": type(error).__name__},
            )
            return
        if not self.mtproto.connected:
            self.logger.warning("MTProto startup did not establish a connection; MsgLog ingestion remains pending.", extra={"event": "telegram_channel.mtproto_disconnected"})
            return
        self.logger.info("Resuming pending MsgLog ingestions", extra={"event": "telegram_channel.msglog_resume"})
        self.chat_binding.resume_pending_msglog_ingestions()

    async def _telegram_runtime_stopped(self, runtime: TelegramPollingRuntime) -> None:
        await self.mtproto.disconnect()
        self.logger.info("MTProto disconnected", extra={"event": "telegram_channel.mtproto_stopped"})

    def poll(self):
        """
        Message polling process.
        """
        self.telegram_runtime.poll()

    def error(self, update: object, context: CallbackContext):
        """
        Print error to console, and send error message to first admin.
        Triggered by python-telegram-bot error callback.
        """
        assert context.error
        error: Exception = context.error
        if "make sure that only one bot instance is running" in str(error):
            now = time.time()
            # Warn the user only from the second time within ``CONFLICTION_TIMEOUT``
            # seconds to suppress isolated warnings.
            # https://github.com/ehForwarderBot/efb-telegram-master/issues/103
            if now - self.last_poll_confliction_time < self.CONFLICTION_TIMEOUT:
                msg = self._("Conflicted polling detected. If this error persists, please ensure you are running only one instance of this Telegram bot.")
                self.logger.critical(msg, extra={"event": "telegram_channel.polling_conflict"})
                self.bot_manager.api.send_message(self.config["admins"][0], msg)
            self.last_poll_confliction_time = now
            return
        if "Invalid server response" in str(error) and not update:
            self.logger.error("Telegram API returned an invalid server response", extra={"event": "telegram_channel.api_invalid_response"})
            return
        # noinspection PyBroadException
        try:
            raise error
        except telegram.error.Forbidden:
            self.logger.error(
                "Telegram authorization failure while handling update (%s).", type(error).__name__, extra={"event": "telegram_channel.authorization_failed", "error_type": type(error).__name__}
            )
        except telegram.error.BadRequest as e:
            assert isinstance(update, Update)
            if e.message == "Message is not modified" and update.callback_query:
                self.logger.error("Telegram callback message was not modified", extra={"event": "telegram_channel.callback_not_modified"})
            else:
                self.logger.exception("Telegram message request failed (%s).", type(error).__name__, extra={"event": "telegram_channel.request_failed", "error_type": type(error).__name__})
                self.bot_manager.api.send_message(
                    self.config["admins"][0],
                    self._("Message request is invalid.\n{error}\n<code>{update}</code>").format(error=html.escape(str(error)), update=html.escape(str(update))),
                    parse_mode="HTML",
                )
        except (telegram.error.TimedOut, telegram.error.NetworkError):
            self.timeout_count += 1
            self.logger.error(
                "Telegram network error #%d while handling update (%s).",
                self.timeout_count,
                type(error).__name__,
                extra={"event": "telegram_channel.network_error", "error_type": type(error).__name__, "retry_count": self.timeout_count},
            )
            if isinstance(update, Update) and isinstance(update.message, Message):
                sync_reply_html(
                    self.bot_manager.api,
                    update.message,
                    self._("This message is not processed due to poor internet environment of the server.\n<code>{code}</code>").format(code=html.escape(str(error))),
                    quote=True,
                )

            timeout_interval = self.flag("network_error_prompt_interval")
            if timeout_interval > 0 and self.timeout_count % timeout_interval == 0:
                self.bot_manager.api.send_message(
                    self.config["admins"][0],
                    self.ngettext(
                        "<b>EFB Telegram Master channel</b>\n"
                        "You may have a poor internet connection on your server. "
                        "Currently {count} network error is detected.\n"
                        "For more details, please refer to the log.",
                        "<b>EFB Telegram Master channel</b>\n"
                        "You may have a poor internet connection on your server. "
                        "Currently {count} network errors are detected.\n"
                        "For more details, please refer to the log.",
                        self.timeout_count,
                    ).format(count=self.timeout_count),
                    parse_mode="HTML",
                )
        except telegram.error.ChatMigrated as e:
            assert isinstance(update, Update)
            new_id = e.new_chat_id
            assert isinstance(update.message, Message)
            old_id = ChatID(str(update.message.chat_id))
            count = 0
            for i in self.db.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, old_id)):
                self.logger.info("Migrating Telegram chat association", extra={"event": "telegram_channel.chat_migrated", "old_chat_id": str(old_id), "new_chat_id": new_id})
                self.db.remove_chat_assoc(slave_uid=i)
                self.db.add_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, ChatID(str(new_id))), slave_uid=i)
                count += 1
            self.bot_manager.api.send_message(
                new_id,
                self.ngettext(
                    "Chat migration detected.\nAll {count} remote chat are now linked to this new group.", "Chat migration detected.\nAll {count} remote chats are now linked to this new group.", count
                ).format(count=count),
            )
        except Exception:
            try:
                self.bot_manager.api.send_message(
                    self.config["admins"][0],
                    self._("EFB Telegram Master channel encountered error <code>{error}</code> caused by update <code>{update}</code>. See log for details.").format(
                        error=html.escape(str(error)), update=html.escape(str(update))
                    ),
                    parse_mode="HTML",
                )
            except Exception as ex:
                self.logger.exception(
                    "Failed to send error message through Telegram (%s).", type(ex).__name__, extra={"event": "telegram_channel.error_notification_failed", "error_type": type(ex).__name__}
                )

            finally:
                self.logger.error(
                    "Unhandled Telegram bot error while handling update (%s).", type(error).__name__, extra={"event": "telegram_channel.unhandled_error", "error_type": type(error).__name__}
                )

    def _is_stopping(self) -> bool:
        bot_manager = getattr(self, "bot_manager", None)
        manager_stopping = getattr(bot_manager, "_stopping", False)
        if hasattr(manager_stopping, "is_set"):
            manager_stopping = manager_stopping.is_set()
        return bool(getattr(self, "_stop_polling_called", False) or manager_stopping)

    def send_message(self, msg: EFBMessage) -> EFBMessage:
        if self._is_stopping():
            return msg
        return self.slave_messages.send_message(msg)

    def send_status(self, status: Status):
        if self._is_stopping():
            return None
        return self.slave_messages.send_status(status)

    def get_message_by_id(self, chat: Chat, msg_id: MessageID) -> Optional[EFBMessage]:
        origin_uid = etm_utils.chat_id_to_str(chat=chat)
        msg_log = self.db.get_msg_log(slave_origin_uid=origin_uid, slave_msg_id=msg_id)
        if msg_log is not None and msg_log.provenance != "mtproto_ingested":
            return msg_log.build_etm_msg(self.chat_manager)
        else:
            # Message is not found.
            return None

    def void_callback_handler(self, update: Update, context: CallbackContext):
        assert isinstance(update, Update)
        assert update.effective_message
        assert update.effective_chat
        assert update.callback_query
        self.bot_manager.api.answer_callback_query(
            update.callback_query.id, text=self._("This button does nothing."), chat_id=update.effective_chat.id, message_id=update.effective_message.message_id, cache_time=180
        )

    def stop_polling(self):
        if getattr(self, "_stop_polling_called", False):
            return
        self._stop_polling_called = True
        self.logger.info("Stopping Telegram channel", extra={"event": "telegram_channel.stop_started"})
        self.rpc_utilities.shutdown()
        self.bot_manager.stop_channel_resources()
        self.telegram_runtime.stop()
        self.master_messages.stop_worker()
        self.db.stop_worker()
        self.logger.info("Stopped Telegram channel", extra={"event": "telegram_channel.stop_completed"})

    def get_chats(self) -> List[Chat]:
        raise EFBOperationNotSupported()

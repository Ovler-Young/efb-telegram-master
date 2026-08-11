# coding=utf-8

import logging
import mimetypes
from typing import Callable, List, Optional
from xmlrpc.server import SimpleXMLRPCServer

from ehforwarderbot import coordinator
from ehforwarderbot.channel import MasterChannel
from ehforwarderbot.chat import Chat
from ehforwarderbot.constants import MsgType
from ehforwarderbot.exceptions import EFBException, EFBOperationNotSupported
from ehforwarderbot.message import Message as EFBMessage
from ehforwarderbot.status import MessageRemoval, Status
from ehforwarderbot.types import InstanceID, MessageID, ModuleID
from PIL import Image, WebPImagePlugin
from telegram import Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, ConversationHandler

from . import utils as etm_utils
from .__version__ import __version__
from .bot_manager import TelegramBotManager, TelegramResourceShutdownError
from .callback_sessions import CallbackSessionStore
from .channel_commands import LocaleState, TelegramCommandService, load_channel_config
from .chat_destination_cache import ChatDestinationCache
from .chat_head import ChatHeadService
from .chat_object_cache import ChatObjectCacheManager
from .commands import CommandsManager
from .constants import Flags
from .db import DatabaseManager
from .history_replay import HistoryReplayWorker
from .link_completion import LinkCompletionService
from .link_service import LinkService
from .master_delivery import MasterMessageDelivery
from .master_inbound import MasterMessageInbound
from .master_message import MasterMessageWorker
from .master_mutations import MasterMessageMutations
from .message import ETMMsg
from .mtproto import MTProtoClient
from .oversized_notice import OversizedNoticeSender
from .ptb_compat import Filters
from .recipient_suggestions import RecipientSuggestionService
from .rpc_utils import RPCUtilities
from .slave_file_delivery import SlaveFileDelivery
from .slave_file_transfer import SlaveFileTransfer
from .slave_image_delivery import ImageDelivery
from .slave_media_delivery import SlaveMediaDelivery
from .slave_message import SlaveMessageService
from .slave_routing import SlaveMessageRouter
from .slave_status import SlaveStatusService
from .slave_text_delivery import TextDelivery
from .topic_sync import TopicGroupService
from .utils import ExperimentalFlagsManager, TelegramChatID


class TelegramChannel(MasterChannel):
    """EFB Telegram master channel."""

    # Meta Info
    channel_name = "Telegram Master"
    channel_emoji = "✈"
    channel_id = ModuleID("blueset.telegram")
    supported_message_types = {MsgType.Text, MsgType.File, MsgType.Voice, MsgType.Image, MsgType.Link, MsgType.Location, MsgType.Sticker, MsgType.Video, MsgType.Animation, MsgType.Status}
    __version__ = __version__

    _stop_polling = False
    config: dict
    message_service: SlaveMessageService
    status_service: SlaveStatusService
    master_message_worker: MasterMessageWorker

    # RPC server
    rpc_server: Optional[SimpleXMLRPCServer] = None

    def __init__(self, instance_id: Optional[InstanceID] = None):
        """
        Initialization.
        """
        super().__init__(instance_id)  # type: ignore[arg-type]  # upstream Channel.__init__ accepts None but isn't annotated as Optional
        self.locale_state = LocaleState()

        # Check PIL support for WebP
        Image.init()
        if "WEBP" not in Image.ID or not getattr(WebPImagePlugin, "SUPPORTED", None):
            raise EFBException(self._("WebP support of Pillow is required.\nPlease refer to Pillow Documentation for instructions.\nhttps://pillow.readthedocs.io/"))

        self.logger: logging.Logger = logging.getLogger(__name__)
        self.config, self.mtproto_config = load_channel_config(self.channel_id, self._)
        mimetypes.init(files=["mimetypes"])
        self.flag: ExperimentalFlagsManager = ExperimentalFlagsManager(self)
        self.db: DatabaseManager = DatabaseManager(self)
        self.chat_associations = self.db.chat_associations
        self.slave_chat_info = self.db.slave_chat_info
        self.msglogs = self.db.msglogs
        self.history_migrations = self.db.history_migrations
        self.msglog_ingestion = self.db.msglog_ingestion
        self.mtproto = MTProtoClient(self.mtproto_config, self.config["token"], self.db._base_path)
        self.chat_manager: ChatObjectCacheManager = ChatObjectCacheManager(self)
        self.chat_dest_cache: ChatDestinationCache = ChatDestinationCache(self.flag("send_to_last_chat"))
        self.topic_group: Optional[TelegramChatID] = TelegramChatID(self.flag("topic_group"))
        self.bot_manager = TelegramBotManager(
            self,
            self.mtproto,
            self.msglog_ingestion,
            self.chat_associations,
            self.channel_id,
            lambda: int(self.flag("network_error_prompt_interval")),
            lambda: bool(self.flag("auto_locale")),
            self._,
            self.ngettext,
            self.locale_state.update,
        )
        self.telegram_runtime = self.bot_manager.telegram_runtime
        self.msglog_scan = self.bot_manager.msglog_scan
        self.history_replay = HistoryReplayWorker(
            self.bot_manager.api,
            self.msglogs,
            self.history_migrations,
            self.chat_manager,
            self.logger,
            self._,
        )
        self.topic_sync = TopicGroupService(
            self.telegram_runtime,
            self.bot_manager.api,
            self.chat_associations,
            self.chat_manager,
            self.channel_id,
            self._,
            self.ngettext,
            self.logger,
        )
        self.commands: CommandsManager = CommandsManager(self)
        self.master_message_delivery = MasterMessageDelivery(
            self.bot_manager.api,
            self.msglogs,
            self.chat_manager,
            self._,
            self.flag,
            self._send_master_message_removal,
            self.logger,
        )
        self.callback_sessions = CallbackSessionStore(self.bot_manager.api, lambda: self.flag("chats_per_page"))
        self.recipient_suggestions = RecipientSuggestionService(
            self.bot_manager.api,
            self.callback_sessions,
            self.chat_manager,
            self.master_message_delivery,
            lambda: self.flag("chats_per_page"),
            self._,
            self.logger,
        )
        self.link_service = LinkService(
            self.bot_manager.api,
            self.telegram_runtime,
            self.channel_id,
            self.flag("multiple_slave_chats"),
            self.msglogs,
            self.chat_associations,
            self.chat_manager,
            self.callback_sessions,
            self.recipient_suggestions.render_chat_list,
            self._,
            self.ngettext,
            self.logger,
        )
        self.link_completion = LinkCompletionService(
            self.bot_manager.api,
            self.channel_id,
            lambda: self.flag("multiple_slave_chats"),
            self.chat_associations,
            self.callback_sessions,
            self.topic_sync,
            self.history_replay,
            self._,
            self.ngettext,
            self.logger,
        )
        self.command_service = TelegramCommandService(
            self.channel_id,
            self.instance_id,
            self.__version__,
            self.bot_manager.api,
            self.chat_associations,
            self.chat_manager,
            self.msglogs,
            self.msglog_scan,
            self.link_completion,
            self.config["admins"],
            self.topic_group,
            self.logger,
            self.locale_state,
        )
        self.chat_head = ChatHeadService(
            self.bot_manager.api,
            self.callback_sessions,
            self.chat_associations,
            self.chat_manager,
            self,
            self.msglogs,
            self.recipient_suggestions.render_chat_list,
            self._,
        )
        non_edit_filter = Filters.update.message | Filters.update.channel_post
        self.telegram_runtime.application.add_handler(CommandHandler("link", self.telegram_runtime.as_async_callback(self.link_service.show_list), filters=non_edit_filter))
        self.link_handler = ConversationHandler(
            entry_points=[],
            states={
                Flags.LINK_CONFIRM: [CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.link_service.confirm))],
                Flags.LINK_EXEC: [CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.link_service.execute))],
            },
            fallbacks=[CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.bot_manager.api.session_expired))],
            per_message=True,
            per_chat=True,
            per_user=False,
        )
        self.link_service.set_handler(self.link_handler)
        self.link_completion.set_handler(self.link_handler)
        self.telegram_runtime.application.add_handler(self.link_handler)
        self.telegram_runtime.application.add_handler(CommandHandler("chat", self.telegram_runtime.as_async_callback(self.chat_head.start_chat_list), filters=non_edit_filter))
        self.chat_head_handler = ConversationHandler(
            entry_points=[],
            states={Flags.CHAT_HEAD_CONFIRM: [CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.chat_head.make_chat_head))]},
            fallbacks=[CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.bot_manager.api.session_expired))],
            per_message=True,
            per_chat=True,
            per_user=False,
        )
        self.chat_head.set_handler(self.chat_head_handler)
        self.telegram_runtime.application.add_handler(self.chat_head_handler)
        self.telegram_runtime.application.add_handler(CommandHandler("unlink_all", self.telegram_runtime.as_async_callback(self.link_completion.unlink_all)))
        self.suggestion_handler = ConversationHandler(
            entry_points=[],
            states={Flags.SUGGEST_RECIPIENTS: [CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.recipient_suggestions.suggested_recipient))]},
            fallbacks=[CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.bot_manager.api.session_expired))],
            per_message=True,
            per_chat=True,
            per_user=False,
        )
        self.recipient_suggestions.set_handler(self.suggestion_handler)
        self.telegram_runtime.application.add_handler(self.suggestion_handler)
        self.topic_sync.register_handlers()
        self.history_replay.resume()
        self.telegram_runtime.application.add_handler(CommandHandler("start", self.telegram_runtime.as_async_callback(self.command_service.start), filters=non_edit_filter))
        self.telegram_runtime.application.add_handler(CommandHandler("help", self.telegram_runtime.as_async_callback(self.command_service.help), filters=non_edit_filter))
        self.telegram_runtime.application.add_handler(CommandHandler("info", self.telegram_runtime.as_async_callback(self.command_service.info), filters=non_edit_filter))
        self.telegram_runtime.application.add_handler(CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.void_callback_handler), pattern="void"))
        self.telegram_runtime.application.add_handler(CallbackQueryHandler(self.telegram_runtime.as_async_callback(self.bot_manager.api.session_expired)))
        self.telegram_runtime.application.add_handler(CommandHandler("react", self.telegram_runtime.as_async_callback(self.command_service.react), filters=non_edit_filter))
        self.telegram_runtime.application.add_handler(CommandHandler("sync_msglog", self.telegram_runtime.as_async_callback(self.command_service.sync_msglog), filters=non_edit_filter))

        api = self.bot_manager.api
        temp_dir = lambda: ExperimentalFlagsManager.get_temp_dir(self)
        router = SlaveMessageRouter(api, self.msglogs, self.chat_associations, self.chat_dest_cache, self.chat_manager, self.config["admins"], self.topic_group, self.topic_sync, self.logger)
        text_delivery = TextDelivery(self.config["admins"][0], api, self._, self.logger)
        file_transfer = SlaveFileTransfer(self.flag, api, self.logger, self._, temp_dir)
        oversized_notice_sender = OversizedNoticeSender(api)
        image_delivery = ImageDelivery(api, self.flag, self.logger, self._, text_delivery, file_transfer, oversized_notice_sender, temp_dir)
        media_delivery = SlaveMediaDelivery(api, self.logger, text_delivery, file_transfer, oversized_notice_sender, temp_dir)
        file_delivery = SlaveFileDelivery(api, self.flag, self.logger, self._, text_delivery, file_transfer, oversized_notice_sender, temp_dir)
        self.message_service = SlaveMessageService(
            api, self.flag, self.msglogs, self.chat_manager, self.commands, self._, self.ngettext, router, text_delivery, image_delivery, media_delivery, file_delivery
        )
        self.status_service = SlaveStatusService(self.logger, self.slave_chat_info, self.chat_manager, self.msglogs, api, self.flag, router, self.message_service, self._)
        inbound = MasterMessageInbound(
            api,
            self.msglogs,
            self.chat_associations,
            self.chat_dest_cache,
            self.chat_manager,
            self.recipient_suggestions,
            self.master_message_delivery,
            self.channel_id,
            self._,
            self.flag,
            self.logger,
        )
        mutations = MasterMessageMutations(api, self.msglogs, self.chat_manager, self._, self.flag, self._send_master_message_removal, self.logger)
        self.master_message_worker = MasterMessageWorker(self.telegram_runtime, api, inbound, mutations, self._, self.logger)

        self.telegram_runtime.application.add_error_handler(self.telegram_runtime.as_async_callback(self.bot_manager.error))

        self.rpc_utilities = RPCUtilities(self)

    def _translate(self, message: str) -> str:
        return self.locale_state.gettext(message)

    def _translate_plural(self, singular: str, plural: str, count: int) -> str:
        return self.locale_state.ngettext(singular, plural, count)

    @property
    def _(self) -> Callable[[str], str]:
        return self._translate

    @property
    def ngettext(self) -> Callable[[str, str, int], str]:
        return self._translate_plural

    @property
    def locale(self) -> Optional[str]:
        return self.locale_state.locale

    def _send_master_message_removal(self, destination, message: ETMMsg) -> None:
        coordinator.send_status(MessageRemoval(source_channel=self, destination_channel=destination, message=message))

    def _is_stopping(self) -> bool:
        bot_manager = getattr(self, "bot_manager", None)
        manager_stopping = getattr(bot_manager, "_stopping", False)
        if hasattr(manager_stopping, "is_set"):
            manager_stopping = manager_stopping.is_set()
        return bool(getattr(self, "_stop_polling_called", False) or manager_stopping)

    def poll(self) -> None:
        self.telegram_runtime.poll()

    def send_message(self, msg: EFBMessage) -> EFBMessage:
        if self._is_stopping():
            return msg
        return self.message_service.send_message(msg)

    def send_status(self, status: Status):
        if self._is_stopping():
            return None
        return self.status_service.send_status(status)

    def get_message_by_id(self, chat: Chat, msg_id: MessageID) -> Optional[EFBMessage]:
        origin_uid = etm_utils.chat_id_to_str(chat=chat)
        msg_log = self.msglogs.get_msg_log(slave_origin_uid=origin_uid, slave_msg_id=msg_id)
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
        shutdown_error = None
        try:
            self.bot_manager.stop_channel_resources()
        except TelegramResourceShutdownError as error:
            shutdown_error = error
            self.logger.warning("Telegram delivery did not stop before the deadline", extra={"event": "telegram_channel.delivery_shutdown_timeout"})
        finally:
            self.master_message_worker.stop_worker()
            self.db.stop_worker()
        self.logger.info("Stopped Telegram channel", extra={"event": "telegram_channel.stop_completed"})
        if shutdown_error is not None:
            raise shutdown_error

    def get_chats(self) -> List[Chat]:
        raise EFBOperationNotSupported()

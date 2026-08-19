# coding=utf-8

from __future__ import annotations

import logging
import mimetypes
import threading
import time
from typing import TYPE_CHECKING, Callable, List, Optional
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
from telegram.ext import CallbackContext

from . import utils as etm_utils
from .__version__ import __version__
from .bot_manager import TelegramBotManager, TelegramResourceShutdownError
from .channel_commands import load_channel_config
from .channel_composition import initialize_channel_components
from .channel_locale import LocaleState
from .db import DatabaseManager
from .message import ETMMsg
from .rpc_utils import RPCUtilities
from .utils import ExperimentalFlagsManager

if TYPE_CHECKING:
    from .bot_manager import TelegramBotManager
    from .chat_object_cache import ChatObjectCacheManager
    from .master_message import MasterMessageWorker
    from .msglog_reconstruction import MsgLogReconstructor
    from .slave_message import SlaveMessageService
    from .slave_status import SlaveStatusService
    from .topic_sync import TopicGroupService
    from .transport.telegram_runtime import TelegramPollingRuntime


class TelegramChannelInitializationCleanup:
    """Own cleanup for a channel whose constructor could not finish."""

    def __init__(self, channel: "TelegramChannel") -> None:
        self._channel = channel

    def retry(self) -> tuple[BaseException, ...]:
        """Retry cleanup after a resource shutdown timeout."""
        return self._channel._stop_after_constructor_failure()


class TelegramChannel(MasterChannel):
    """EFB Telegram master channel."""

    # Meta Info
    channel_name = "Telegram Master"
    channel_emoji = "✈"
    channel_id = ModuleID("blueset.telegram")
    supported_message_types = {MsgType.Text, MsgType.File, MsgType.Voice, MsgType.Image, MsgType.Link, MsgType.Location, MsgType.Sticker, MsgType.Video, MsgType.Animation, MsgType.Status}
    __version__ = __version__

    _stop_polling = False
    SHUTDOWN_TIMEOUT = TelegramBotManager.SHUTDOWN_DRAIN_TIMEOUT + TelegramBotManager.SHUTDOWN_JOIN_GRACE
    config: dict
    bot_manager: TelegramBotManager
    telegram_runtime: TelegramPollingRuntime
    chat_manager: ChatObjectCacheManager
    topic_sync: TopicGroupService
    message_service: SlaveMessageService
    status_service: SlaveStatusService
    master_message_worker: MasterMessageWorker
    message_reconstructor: MsgLogReconstructor
    # RPC server
    rpc_server: Optional[SimpleXMLRPCServer] = None

    def __init__(self, instance_id: Optional[InstanceID] = None):
        """
        Initialization.
        """
        super().__init__(instance_id)  # type: ignore[arg-type]  # upstream Channel.__init__ accepts None but isn't annotated as Optional
        self._shutdown_lock = threading.Lock()
        self._stopping = False
        self._shutdown_complete = False
        self._stopped_resources: set[str] = set()
        self.locale_state = LocaleState()
        self._owned_database: Optional[DatabaseManager] = None
        self._owned_bot_manager = None
        self._owned_history_replay = None
        self._owned_master_message_worker = None
        self._owned_rpc_utilities = None

        # Check PIL support for WebP
        Image.init()
        if "WEBP" not in Image.ID or not getattr(WebPImagePlugin, "SUPPORTED", None):
            raise EFBException(self._("WebP support of Pillow is required.\nPlease refer to Pillow Documentation for instructions.\nhttps://pillow.readthedocs.io/"))

        self.logger: logging.Logger = logging.getLogger(__name__)
        self.config, self.mtproto_config = load_channel_config(self.channel_id, self._)
        mimetypes.init(files=["mimetypes"])
        self.flag: ExperimentalFlagsManager = ExperimentalFlagsManager(self)
        self.db: DatabaseManager = DatabaseManager(self)
        self._owned_database = self.db
        self.chat_associations = self.db.chat_associations
        self.slave_chat_info = self.db.slave_chat_info
        self.slave_message_deliveries = self.db.slave_message_deliveries
        self.msglogs = self.db.msglogs
        self.history_migrations = self.db.history_migrations
        self.msglog_ingestion = self.db.msglog_ingestion
        try:
            self.rpc_utilities = RPCUtilities(self.config.get("rpc"), self.db, coordinator)
            self._owned_rpc_utilities = self.rpc_utilities
            self.rpc_utilities.start()
            initialize_channel_components(self)
        except BaseException as error:
            cleanup = TelegramChannelInitializationCleanup(self)
            cleanup_errors = cleanup.retry()
            if cleanup_errors:
                setattr(error, "telegram_channel_cleanup", cleanup)
            raise

    def _stop_after_constructor_failure(self) -> tuple[BaseException, ...]:
        lock = getattr(self, "_shutdown_lock", None)
        if lock is None:
            lock = self._shutdown_lock = threading.Lock()
        with lock:
            if getattr(self, "_shutdown_complete", False):
                return ()
            deadline = time.monotonic() + self.SHUTDOWN_TIMEOUT
            master_message_worker = self._shutdown_resource("master_message_worker")
            if master_message_worker is not None:
                try:
                    master_errors = master_message_worker.stop_worker(deadline=deadline)
                except BaseException as error:
                    master_errors = (error,)
                if master_errors:
                    for shutdown_error in master_errors:
                        self.logger.error(
                            "Master message worker did not stop after channel construction failed; retaining dependent resources for cleanup.",
                            exc_info=(type(shutdown_error), shutdown_error, shutdown_error.__traceback__),
                        )
                    return tuple(master_errors)

            errors = self._stop_non_master_resources(deadline)
            if errors:
                return errors
            self._shutdown_complete = True
            return ()

    def _shutdown_resource(self, name: str):
        resource = getattr(self, f"_owned_{name}", None)
        if resource is not None:
            return resource
        attribute = "db" if name == "database" else name
        return getattr(self, attribute, None)

    def _stop_resource(self, name: str, method_name: str, deadline: float) -> tuple[BaseException, ...]:
        stopped_resources = getattr(self, "_stopped_resources", None)
        if stopped_resources is None:
            stopped_resources = self._stopped_resources = set()
        if name in stopped_resources:
            return ()
        resource = self._shutdown_resource(name)
        method = getattr(resource, method_name, None)
        if method is None:
            stopped_resources.add(name)
            return ()
        try:
            if name == "history_replay":
                result = method(max(0.0, deadline - time.monotonic()))
            elif name in {"bot_manager", "rpc_utilities"}:
                result = method(deadline)
            else:
                result = method()
        except TelegramResourceShutdownError as error:
            return error.errors
        except BaseException as error:
            self.logger.exception("Failed to stop %s.", type(resource).__name__)
            return (error,)
        errors = result if isinstance(result, tuple) else ()
        if not errors:
            stopped_resources.add(name)
        return errors

    def _stop_non_master_resources(self, deadline: float) -> tuple[BaseException, ...]:
        initial_history_errors = self._stop_resource("history_replay", "stop", deadline)
        errors = list(self._stop_resource("rpc_utilities", "stop", deadline))
        errors.extend(self._stop_resource("bot_manager", "stop_channel_resources", deadline))
        final_history_errors = self._stop_resource("history_replay", "stop", deadline) if initial_history_errors else ()
        if final_history_errors:
            errors.extend(initial_history_errors)
            errors.extend(final_history_errors)
        if errors:
            return tuple(errors)
        return self._stop_resource("database", "stop_worker", deadline)

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
            return self.message_reconstructor.build(msg_log)
        # MTProto-ingested records have no reconstructable EFB message.
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
        lock = getattr(self, "_shutdown_lock", None)
        if lock is None:
            lock = self._shutdown_lock = threading.Lock()
            self._stopping = self._shutdown_complete = False
        with lock:
            if self._shutdown_complete:
                return
            self._stop_polling_called = self._stopping = True
            deadline = time.monotonic() + self.SHUTDOWN_TIMEOUT
            self.logger.info("Stopping Telegram channel", extra={"event": "telegram_channel.stop_started"})
            master_errors = self._shutdown_resource("master_message_worker").stop_worker(deadline=deadline)
            if master_errors:
                self.logger.warning("Master message worker did not stop before the deadline", extra={"event": "telegram_channel.master_message_shutdown_timeout"})
                raise TelegramResourceShutdownError(master_errors)
            errors = self._stop_non_master_resources(deadline)
            if errors:
                self.logger.warning("Telegram channel stopped with resource errors", extra={"event": "telegram_channel.stop_incomplete"})
                raise TelegramResourceShutdownError(tuple(errors))
            self._shutdown_complete = True
            self.logger.info("Stopped Telegram channel", extra={"event": "telegram_channel.stop_completed"})

    def get_chats(self) -> List[Chat]:
        raise EFBOperationNotSupported()

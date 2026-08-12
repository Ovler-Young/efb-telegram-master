"""Construction of Telegram-channel collaborators and handlers."""

from __future__ import annotations

from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler

from .bot_manager import TelegramBotManager
from .callback_sessions import CallbackSessionStore
from .channel_commands import TelegramCommandService
from .chat_destination_cache import ChatDestinationCache
from .chat_head import ChatHeadService
from .chat_object_cache import ChatObjectCacheManager
from .commands import CommandsManager
from .constants import Flags
from .history_replay import HistoryReplayWorker
from .link_completion import LinkCompletionService
from .link_service import LinkService
from .master_delivery import MasterMessageDelivery
from .master_inbound import MasterMessageInbound
from .master_message import MasterMessageWorker
from .master_mutations import MasterMessageMutations
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


def initialize_channel_components(channel) -> None:
    """Build channel-owned collaborators after its database is ready."""
    channel.mtproto = MTProtoClient(channel.mtproto_config, channel.config["token"], channel.db._base_path)
    channel.chat_manager = ChatObjectCacheManager(channel)
    channel.chat_dest_cache = ChatDestinationCache(channel.flag("send_to_last_chat"))
    channel.topic_group = TelegramChatID(channel.flag("topic_group"))
    channel.bot_manager = TelegramBotManager(
        channel,
        channel.mtproto,
        channel.msglog_ingestion,
        channel.chat_associations,
        channel.channel_id,
        lambda: int(channel.flag("network_error_prompt_interval")),
        lambda: bool(channel.flag("auto_locale")),
        channel._,
        channel.ngettext,
        channel.locale_state.update,
    )
    channel._owned_bot_manager = channel.bot_manager
    channel.telegram_runtime = channel.bot_manager.telegram_runtime
    channel.msglog_scan = channel.bot_manager.msglog_scan
    channel.history_replay = HistoryReplayWorker(
        channel.bot_manager.api,
        channel.msglogs,
        channel.history_migrations,
        channel.chat_manager,
        channel.logger,
        channel._,
    )
    channel._owned_history_replay = channel.history_replay
    channel.topic_sync = TopicGroupService(
        channel.telegram_runtime,
        channel.bot_manager.api,
        channel.chat_associations,
        channel.chat_manager,
        channel.channel_id,
        channel._,
        channel.ngettext,
        channel.logger,
    )
    channel.commands = CommandsManager(channel)
    channel.master_message_delivery = MasterMessageDelivery(
        channel.bot_manager.api,
        channel.msglogs,
        channel.chat_manager,
        channel._,
        channel.flag,
        channel._send_master_message_removal,
        channel.logger,
    )
    channel.callback_sessions = CallbackSessionStore(channel.bot_manager.api, lambda: channel.flag("chats_per_page"))
    channel.recipient_suggestions = RecipientSuggestionService(
        channel.bot_manager.api,
        channel.callback_sessions,
        channel.chat_manager,
        channel.master_message_delivery,
        lambda: channel.flag("chats_per_page"),
        channel._,
        channel.logger,
    )
    channel.link_service = LinkService(
        channel.bot_manager.api,
        channel.telegram_runtime,
        channel.channel_id,
        channel.flag("multiple_slave_chats"),
        channel.msglogs,
        channel.chat_associations,
        channel.chat_manager,
        channel.callback_sessions,
        channel.recipient_suggestions.render_chat_list,
        channel._,
        channel.ngettext,
        channel.logger,
    )
    channel.link_completion = LinkCompletionService(
        channel.bot_manager.api,
        channel.channel_id,
        lambda: channel.flag("multiple_slave_chats"),
        channel.chat_associations,
        channel.callback_sessions,
        channel.topic_sync,
        channel.history_replay,
        channel._,
        channel.ngettext,
        channel.logger,
    )
    channel.command_service = TelegramCommandService(
        channel.channel_id,
        channel.instance_id,
        channel.__version__,
        channel.bot_manager.api,
        channel.chat_associations,
        channel.chat_manager,
        channel.msglogs,
        channel.msglog_scan,
        channel.link_completion,
        channel.config["admins"],
        channel.topic_group,
        channel.logger,
        channel.locale_state,
    )
    channel.chat_head = ChatHeadService(
        channel.bot_manager.api,
        channel.callback_sessions,
        channel.chat_associations,
        channel.chat_manager,
        channel,
        channel.msglogs,
        channel.recipient_suggestions.render_chat_list,
        channel._,
    )
    _register_handlers(channel)
    _build_slave_services(channel)
    channel.telegram_runtime.application.add_error_handler(channel.telegram_runtime.as_async_callback(channel.bot_manager.error))
    channel.rpc_utilities = RPCUtilities(channel)


def _register_handlers(channel) -> None:
    non_edit_filter = Filters.update.message | Filters.update.channel_post
    channel.telegram_runtime.application.add_handler(CommandHandler("link", channel.telegram_runtime.as_async_callback(channel.link_service.show_list), filters=non_edit_filter))
    channel.link_handler = ConversationHandler(
        entry_points=[],
        states={
            Flags.LINK_CONFIRM: [CallbackQueryHandler(channel.telegram_runtime.as_async_callback(channel.link_service.confirm))],
            Flags.LINK_EXEC: [CallbackQueryHandler(channel.telegram_runtime.as_async_callback(channel.link_service.execute))],
        },
        fallbacks=[CallbackQueryHandler(channel.telegram_runtime.as_async_callback(channel.bot_manager.api.session_expired))],
        per_message=True,
        per_chat=True,
        per_user=False,
    )
    channel.link_service.set_handler(channel.link_handler)
    channel.link_completion.set_handler(channel.link_handler)
    channel.telegram_runtime.application.add_handler(channel.link_handler)
    channel.telegram_runtime.application.add_handler(CommandHandler("chat", channel.telegram_runtime.as_async_callback(channel.chat_head.start_chat_list), filters=non_edit_filter))
    channel.chat_head_handler = ConversationHandler(
        entry_points=[],
        states={Flags.CHAT_HEAD_CONFIRM: [CallbackQueryHandler(channel.telegram_runtime.as_async_callback(channel.chat_head.make_chat_head))]},
        fallbacks=[CallbackQueryHandler(channel.telegram_runtime.as_async_callback(channel.bot_manager.api.session_expired))],
        per_message=True,
        per_chat=True,
        per_user=False,
    )
    channel.chat_head.set_handler(channel.chat_head_handler)
    channel.telegram_runtime.application.add_handler(channel.chat_head_handler)
    channel.telegram_runtime.application.add_handler(CommandHandler("unlink_all", channel.telegram_runtime.as_async_callback(channel.link_completion.unlink_all)))
    channel.suggestion_handler = ConversationHandler(
        entry_points=[],
        states={Flags.SUGGEST_RECIPIENTS: [CallbackQueryHandler(channel.telegram_runtime.as_async_callback(channel.recipient_suggestions.suggested_recipient))]},
        fallbacks=[CallbackQueryHandler(channel.telegram_runtime.as_async_callback(channel.bot_manager.api.session_expired))],
        per_message=True,
        per_chat=True,
        per_user=False,
    )
    channel.recipient_suggestions.set_handler(channel.suggestion_handler)
    channel.telegram_runtime.application.add_handler(channel.suggestion_handler)
    channel.topic_sync.register_handlers()
    channel.history_replay.resume()
    channel.telegram_runtime.application.add_handler(CommandHandler("start", channel.telegram_runtime.as_async_callback(channel.command_service.start), filters=non_edit_filter))
    channel.telegram_runtime.application.add_handler(CommandHandler("help", channel.telegram_runtime.as_async_callback(channel.command_service.help), filters=non_edit_filter))
    channel.telegram_runtime.application.add_handler(CommandHandler("info", channel.telegram_runtime.as_async_callback(channel.command_service.info), filters=non_edit_filter))
    channel.telegram_runtime.application.add_handler(CallbackQueryHandler(channel.telegram_runtime.as_async_callback(channel.void_callback_handler), pattern="void"))
    channel.telegram_runtime.application.add_handler(CallbackQueryHandler(channel.telegram_runtime.as_async_callback(channel.bot_manager.api.session_expired)))
    channel.telegram_runtime.application.add_handler(CommandHandler("react", channel.telegram_runtime.as_async_callback(channel.command_service.react), filters=non_edit_filter))
    channel.telegram_runtime.application.add_handler(CommandHandler("sync_msglog", channel.telegram_runtime.as_async_callback(channel.command_service.sync_msglog), filters=non_edit_filter))


def _build_slave_services(channel) -> None:
    api = channel.bot_manager.api
    temp_dir = lambda: ExperimentalFlagsManager.get_temp_dir(channel)
    router = SlaveMessageRouter(
        api, channel.msglogs, channel.chat_associations, channel.chat_dest_cache, channel.chat_manager, channel.config["admins"], channel.topic_group, channel.topic_sync, channel.logger
    )
    text_delivery = TextDelivery(channel.config["admins"][0], api, channel._, channel.logger)
    file_transfer = SlaveFileTransfer(channel.flag, api, channel.logger, channel._, temp_dir)
    oversized_notice_sender = OversizedNoticeSender(api)
    image_delivery = ImageDelivery(api, channel.flag, channel.logger, channel._, text_delivery, file_transfer, oversized_notice_sender, temp_dir)
    media_delivery = SlaveMediaDelivery(api, channel.logger, text_delivery, file_transfer, oversized_notice_sender, temp_dir)
    file_delivery = SlaveFileDelivery(api, channel.flag, channel.logger, channel._, text_delivery, file_transfer, oversized_notice_sender, temp_dir)
    channel.message_service = SlaveMessageService(
        api, channel.flag, channel.msglogs, channel.chat_manager, channel.commands, channel._, channel.ngettext, router, text_delivery, image_delivery, media_delivery, file_delivery
    )
    channel.status_service = SlaveStatusService(channel.logger, channel.slave_chat_info, channel.chat_manager, channel.msglogs, api, channel.flag, router, channel.message_service, channel._)
    inbound = MasterMessageInbound(
        api,
        channel.msglogs,
        channel.chat_associations,
        channel.chat_dest_cache,
        channel.chat_manager,
        channel.recipient_suggestions,
        channel.master_message_delivery,
        channel.channel_id,
        channel._,
        channel.flag,
        channel.logger,
    )
    mutations = MasterMessageMutations(api, channel.msglogs, channel.chat_manager, channel._, channel.flag, channel._send_master_message_removal, channel.logger)
    channel.master_message_worker = MasterMessageWorker(channel.telegram_runtime, api, inbound, mutations, channel._, channel.logger)
    channel._owned_master_message_worker = channel.master_message_worker

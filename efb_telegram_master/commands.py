# coding=utf-8
import html
import logging
from typing import Callable, Collection, Dict, List, Optional, Tuple, Union, cast

from ehforwarderbot import Channel, Middleware
from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.message import MessageCommand
from ehforwarderbot.types import ExtraCommandName
from telegram import Message, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler
from telegram.ext._utils.types import ConversationDict

from .constants import Flags
from .ptb_compat import Filters


class ETMCommandMsgStorage:
    def __init__(self, commands: List[MessageCommand], module: Union[Channel, Middleware], prefix: str, body: str, authorized_user_ids: Collection[int]):
        self.commands = commands
        self.module = module
        self.prefix = prefix
        self.body = body
        self.authorized_user_ids = frozenset(authorized_user_ids)

    def __str__(self):
        return f"ETMCommandMsgStorage({self.commands!r}, {self.module!r}, {self.prefix!r}, {self.body!r}, {self.authorized_user_ids!r})"


class CommandsManager:
    """
    Functions related to Command messages and
    Additional features of slave channels.
    """

    def __init__(self, bot, runtime, localize: Callable[[str], str], modules: Callable[[], Collection[Union[SlaveChannel, Middleware]]]):
        self.bot = bot
        self.runtime = runtime
        self._ = localize
        self.modules = modules
        self.msg_storage: Dict[Tuple[int, int], ETMCommandMsgStorage] = dict()
        self.logger = logging.getLogger(__name__)

        self.runtime.application.add_handler(CommandHandler("extra", self.runtime.as_async_callback(self.extra_listing)))
        self.runtime.application.add_handler(MessageHandler(Filters.regex(r"^/h_(?P<id>[0-9]+)_(?P<command>[a-z0-9_-]+)"), self.runtime.as_async_callback(self.extra_usage)))
        self.runtime.application.add_handler(MessageHandler(Filters.regex(r"^/(?P<id>[0-9]+)_(?P<command>[a-z0-9_-]+)"), self.runtime.as_async_callback(self.extra_call)))

        self.command_conv = ConversationHandler(
            entry_points=[],
            states={Flags.COMMAND_PENDING: [CallbackQueryHandler(self.runtime.as_async_callback(self.command_exec))]},
            fallbacks=[CallbackQueryHandler(self.runtime.as_async_callback(self.bot.session_expired))],
            per_message=True,
            per_chat=True,
            per_user=False,
        )

        self.runtime.application.add_handler(self.command_conv)

        self.modules_list = list(self.modules())

    def register_command(self, message: Message, commands: ETMCommandMsgStorage):
        message_identifier = (message.chat.id, message.message_id)
        conversations = getattr(self.command_conv, "_conversations", None)
        if conversations is None:
            conversations = getattr(self.command_conv, "conversations")
        conversations = cast(ConversationDict, conversations)
        conversations[cast(Tuple[int, ...], message_identifier)] = Flags.COMMAND_PENDING
        self.msg_storage[message_identifier] = commands

    def command_exec(self, update: Update, context: CallbackContext) -> Optional[int]:
        """
        Run a command from a command message.
        Triggered by callback message with status `Flags.COMMAND_PENDING`.

        This method is a part of the command message conversation handler.

        Returns:
            The next state
        """
        assert isinstance(update, Update)
        assert update.effective_chat
        assert update.effective_message
        assert update.callback_query
        assert update.effective_user

        chat_id = update.effective_chat.id
        message_id = update.effective_message.message_id
        callback = update.callback_query.data

        assert callback

        index = (chat_id, message_id)

        command_storage = self.msg_storage[index]
        if update.callback_query.from_user.id != update.effective_user.id or update.effective_user.id not in command_storage.authorized_user_ids:
            self.bot.answer_callback_query(callback_query_id=update.callback_query.id, text=self._("Session expired or unknown parameter. (SE02)"))
            return Flags.COMMAND_PENDING

        if not callback.isdecimal():
            msg = self._("Invalid parameter: {0}. (CE01)").format(callback)
            self.msg_storage.pop(index, None)
            self.bot.edit_message_text(text=msg, chat_id=chat_id, message_id=message_id)
            self.bot.answer_callback_query(callback_query_id=update.callback_query.id)
            return ConversationHandler.END
        elif not (0 <= int(callback) < len(self.msg_storage[index].commands)):
            msg = self._("Index out of bound: {0}. (CE02)").format(callback)
            self.msg_storage.pop(index, None)
            self.bot.edit_message_text(text=msg, chat_id=chat_id, message_id=message_id)
            self.bot.answer_callback_query(callback_query_id=update.callback_query.id)
            return ConversationHandler.END

        callback_idx = int(callback)
        module = command_storage.module
        command = command_storage.commands[callback_idx]
        prefix = command_storage.prefix

        self.logger.debug("[%s.%s] Command execution callback is valid. Command storage item: %s", chat_id, message_id, command_storage)

        # Clear inline buttons.
        self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        self.logger.debug("[%s.%s] Inline buttons cleared", chat_id, message_id)

        fn = getattr(module, command.callable_name, None)
        if fn is not None:
            msg = fn(*command.args, **command.kwargs)
        else:
            module_id = str(module)
            if isinstance(module, Channel):
                module_id = module.channel_id
            elif isinstance(module, Middleware):
                module_id = module.middleware_id
            msg = self._command_fallback(
                *command.args,  # type: ignore
                __channel_id=module_id,
                __callable=command.callable_name,
                **command.kwargs,
            )
        self.logger.debug("[%s.%s] Command execution outcome: %s", chat_id, message_id, msg)
        if msg is not None:
            self.msg_storage.pop(index, None)
        # self.bot.edit_message_text(prefix=prefix, text=msg,
        #                            chat_id=chat_id, message_id=message_id)
        if msg is None:
            self.bot.answer_callback_query(callback_query_id=update.callback_query.id)
            return None
        self.bot.answer_callback_query(
            prefix=prefix,
            text=msg,
            callback_query_id=update.callback_query.id,
            chat_id=update.effective_chat.id,
            message_id=update.effective_message.message_id,
        )
        return ConversationHandler.END

    def extra_listing(self, update: Update, context: CallbackContext):
        """
        Show list of additional features and their usage.
        Triggered by `/extra`.
        """
        assert isinstance(update, Update)
        assert update.effective_chat

        msg = self._("<i>Click the link next to the name for usage.</i>\n")
        for idx, i in enumerate(self.modules_list):
            if isinstance(i, Channel):
                msg += "\n\n<b>{0} {1}".format(html.escape(i.channel_emoji), html.escape(i.channel_name))
                if i.instance_id:
                    msg += " ({})".format(html.escape(i.instance_id))
                msg += "</b>"

            elif isinstance(i, Middleware):
                msg += "\n\n<b>{} ({})</b>".format(html.escape(i.middleware_name), html.escape(i.middleware_id))
            else:
                # This should not occur as modules_list shall
                # consist of only Channel and Middleware instances
                continue
            extra_fns = i.get_extra_functions()
            if extra_fns:
                for fn in extra_fns:
                    fn_name = f"/h_{idx}_{fn}"
                    # noinspection PyUnresolvedReferences
                    msg += "\n- <b>{}</b> {}".format(html.escape(cast(MessageCommand, extra_fns[fn]).name), html.escape(fn_name))
            else:
                msg += "\n" + self._("No command found.")
        self.bot.send_message(update.effective_chat.id, msg, parse_mode="HTML")

    def extra_usage(self, update: Update, context: CallbackContext):
        assert context.match
        assert isinstance(update, Update)
        assert update.effective_chat

        groupdict = context.match.groupdict()
        if int(groupdict["id"]) >= len(self.modules_list):
            return self.bot.reply_error(update, self._("Invalid module ID. (XC03)"))

        channel = self.modules_list[int(groupdict["id"])]
        functions = channel.get_extra_functions()

        if groupdict["command"] not in functions:
            return self.bot.reply_error(update, self._("Command not found in selected module. (XC04)"))

        command = cast(MessageCommand, getattr(channel, groupdict["command"]))

        if isinstance(channel, Channel):
            msg = "<b>{0} {1}".format(html.escape(channel.channel_emoji), html.escape(channel.channel_name))
            if channel.instance_id:
                msg += " ({})".format(html.escape(channel.instance_id))
            msg += "</b>"
        else:
            msg = "<b>{0} ({1})</b>".format(
                html.escape(channel.middleware_name),
                html.escape(channel.middleware_id),
            )

        fn_name = "/%s_%s" % (groupdict["id"], groupdict["command"])
        command_description = cast(str, getattr(command, "desc"))
        msg += "\n\n{} <b>({})</b>\n{}".format(
            html.escape(fn_name),
            html.escape(command.name),
            html.escape(command_description.format(function_name=fn_name)),
        )
        self.bot.send_message(update.effective_chat.id, msg, parse_mode="HTML")

    def extra_call(self, update: Update, context: CallbackContext):
        """
        Invoke an additional feature from slave channel.
        """
        assert context.match
        assert isinstance(update, Update)
        assert update.message

        groupdict = context.match.groupdict()
        modules = self.modules()
        if int(groupdict["id"]) >= len(modules):
            return self.bot.reply_error(update, self._("Invalid module ID. (XC01)"))

        channel = modules[int(groupdict["id"])]
        functions = channel.get_extra_functions()

        if groupdict["command"] not in functions:
            return self.bot.reply_error(update, self._("Command not found in selected module. (XC02)"))

        # noinspection PyUnresolvedReferences
        header = "{} {}: {}\n-------\n".format(
            channel.channel_emoji,
            channel.channel_name,
            functions[groupdict["command"]].name,  # type: ignore
        )
        msg = self.bot.send_message(update.message.chat.id, prefix=header, text=self._("Please wait..."))

        assert update.message.text
        result = functions[ExtraCommandName(groupdict["command"])](" ".join(update.message.text.split(" ", 1)[1:]))

        self.bot.edit_message_text(prefix=header, text=result, chat_id=update.message.chat.id, message_id=msg.message_id)

    def _command_fallback(self, *args, __channel_id: str, __callable: str, **kwargs) -> str:
        return self._("Error: Command is not found in the channel.\nFunction: {channel_id}.{callable}\nArguments: {args!r}\nKeyword Arguments: {kwargs!r}").format(
            channel_id=__channel_id, callable=__callable, args=args, kwargs=kwargs
        )

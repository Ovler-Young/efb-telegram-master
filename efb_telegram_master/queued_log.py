"""Log-only message snapshots. Recovery must never fetch or reopen media."""

import gc
import io
import pickle
from types import SimpleNamespace

from ehforwarderbot.chat import ChatMember, GroupChat
from ehforwarderbot.message import Substitutions

from .message import ETMMsg


class LogMessage(ETMMsg):
    """Only used for durable MsgLog input, never as a media delivery object."""

    def __setstate__(self, state):
        # Message.__setstate__ accesses self.path, which calls ETMMsg._load_file.
        # For historical contexts that can download/transcode an entire quoted
        # attachment merely to write a log. Restore state without those effects.
        self.__dict__.update(state)
        destination = state.get("deliver_to")
        if isinstance(destination, str):
            self.deliver_to = SimpleNamespace(channel_id=destination)
        self._ETMMsg__initialized = True


class LogUnpickler(pickle.Unpickler):
    """Read trusted old queue contexts without EFB's media restoration hooks."""

    def find_class(self, module, name):
        if (module, name) in {
            ("efb_telegram_master.message", "ETMMsg"),
            ("ehforwarderbot.message", "Message"),
        }:
            return LogMessage
        return super().find_class(module, name)


def _identity(chat):
    """Preserve the exact ID tuple, not group members or vendor-side caches."""
    if isinstance(chat, ChatMember):
        group = GroupChat(module_id=chat.module_id, uid=chat.chat.uid, with_self=False)
        return ChatMember(group, uid=chat.uid)
    return GroupChat(module_id=chat.module_id, uid=chat.uid, with_self=False)


def snapshot(message: ETMMsg) -> LogMessage:
    # These are precisely the message inputs consumed by add_or_update_message_log
    # and pickle_misc_msg. In particular, do not copy __dict__, file/path, vendor
    # data, or the recursively quoted message graph.
    result = LogMessage(
        uid=message.uid, text=message.text,
        chat=_identity(message.chat), author=_identity(message.author),
        type=message.type, type_telegram=message.type_telegram,
        mime=message.mime, is_system=message.is_system,
        attributes=message.attributes, commands=message.commands,
        deliver_to=message.deliver_to,
    )
    result.file_id = message.file_id
    result.file_unique_id = message.file_unique_id
    result.sender_bot_id = message.sender_bot_id
    result._ETMMsg__initialized = True
    if message.target:
        result.target = LogMessage(uid=message.target.uid, chat=_identity(message.target.chat))
    if message.substitutions:
        result.substitutions = Substitutions({key: _identity(chat) for key, chat in message.substitutions.items()})
    if message.reactions:
        result.reactions = {key: tuple(_identity(chat) for chat in chats) for key, chats in message.reactions.items()}
    return result


def encode(message: ETMMsg, old_message_id) -> bytes:
    return b"\x02" + pickle.dumps((snapshot(message), old_message_id), protocol=5)


def decode(payload: bytes):
    # BytesIO avoids slicing a potentially large historical context just to
    # discard its one-byte version prefix. No writer mutates this buffer.
    with io.BytesIO(payload) as stream:
        stream.seek(1)
        value = LogUnpickler(stream).load()
    if not isinstance(value, tuple) or len(value) != 2 or not isinstance(value[0], ETMMsg):
        raise ValueError("Queued database log context has an invalid shape.")
    if payload[0] == 1:
        # Old objects contain member<->group cycles. Release them *now*, rather
        # than retaining several huge legacy graphs until an unrelated GC run.
        compact = snapshot(value[0]), value[1]
        del value
        gc.collect()
        return compact
    return value

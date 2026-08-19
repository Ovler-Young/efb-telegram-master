from contextlib import contextmanager
from copy import copy
from io import BytesIO
from queue import Queue
from typing import Dict, List, Tuple
from uuid import uuid4

from ehforwarderbot import Chat, Message, Status, coordinator
from ehforwarderbot.chat import ChatMember, SelfChatMember
from ehforwarderbot.exceptions import EFBMessageReactionNotPossible, EFBOperationNotSupported
from ehforwarderbot.status import ChatUpdates, MemberUpdates, MessageReactionsUpdate, MessageRemoval, ReactToMessage
from ehforwarderbot.types import MessageID, ReactionName

from .types import ReactionMode


class DeliveryMixin:
    def clear_messages(self):
        self._clear_queue(self.messages)

    def clear_statuses(self):
        self._clear_queue(self.statuses)

    @staticmethod
    def _clear_queue(queue: Queue):
        with queue.mutex:
            unfinished = queue.unfinished_tasks - len(queue.queue)
            if unfinished <= 0:
                if unfinished < 0:
                    raise ValueError("task_done() called too many times")
                queue.all_tasks_done.notify_all()
            queue.unfinished_tasks = unfinished
            queue.queue.clear()
            queue.not_full.notify_all()

    def poll(self):
        self.polling.wait()

    def send_status(self, status: Status):
        self.logger.debug("Received status: %r", status)
        if isinstance(status, MessageRemoval):
            self.message_removal_status(status)
        elif isinstance(status, ReactToMessage):
            self.react_to_message_status(status)
        self.statuses.put(status)

    def send_message(self, msg: Message) -> Message:
        self.logger.debug("Received message: uid=%r type=%r chat=%r edit=%r edit_media=%r", msg.uid, msg.type, getattr(msg.chat, "uid", None), msg.edit, msg.edit_media)
        msg.uid = MessageID(str(uuid4()))
        self._store_message(msg)
        self.messages.put(self._snapshot_message(msg))
        return msg

    @staticmethod
    def _snapshot_message(message: Message) -> Message:
        snapshot = copy(message)
        if message.file is None:
            return snapshot
        position = message.file.tell()
        try:
            message.file.seek(0)
            snapshot.file = BytesIO(message.file.read())
        finally:
            message.file.seek(position)
        return snapshot

    def stop_polling(self):
        self.polling.set()

    def _store_message(self, message: Message) -> None:
        if message.uid is None:
            raise ValueError("Mock messages must have a MessageID before storing them.")
        self.messages_sent[message.uid] = message

    def message_removal_status(self, status: MessageRemoval):
        if not self.message_removal_possible:
            raise EFBOperationNotSupported("Message removal is not possible by flag.")

    @contextmanager
    def set_message_removal(self, value: bool):
        backup = self.message_removal_possible
        self.message_removal_possible = value
        try:
            yield
        finally:
            self.message_removal_possible = backup

    def react_to_message_status(self, status: ReactToMessage):
        if self.accept_message_reactions == "reject_one":
            raise EFBMessageReactionNotPossible("Message reaction is rejected by flag.")
        if self.accept_message_reactions == "reject_all":
            raise EFBOperationNotSupported("All message reactions are rejected by flag.")
        message = self.messages_sent.get(MessageID(status.msg_id))
        if message is None:
            raise EFBOperationNotSupported("Message is not found.")
        if status.reaction is None:
            updated_reactions: Dict[ReactionName, List[ChatMember]] = {
                reaction: [member for member in members if not isinstance(member, SelfChatMember)] for reaction, members in (message.reactions or {}).items()
            }
        else:
            updated_reactions = {reaction: list(members) for reaction, members in (message.reactions or {}).items()}
            self_member = message.chat.self
            assert self_member is not None
            updated_reactions.setdefault(ReactionName(status.reaction), []).append(self_member)
        message.reactions = updated_reactions
        assert message.uid is not None
        coordinator.send_status(MessageReactionsUpdate(chat=message.chat, msg_id=message.uid, reactions=updated_reactions))

    @contextmanager
    def set_react_to_message(self, value: ReactionMode):
        backup = self.accept_message_reactions
        self.accept_message_reactions = value
        try:
            yield
        finally:
            self.accept_message_reactions = backup

    def send_chat_update_status(self) -> Tuple[Chat, Chat, Chat]:
        keyword = " (Edited)"
        if self.backup_chat not in self.chats:
            to_add, to_remove = self.backup_chat, self.chat_to_toggle
            self.chat_to_edit.name += keyword
        else:
            to_add, to_remove = self.chat_to_toggle, self.backup_chat
            self.chat_to_edit.name = self.chat_to_edit.name.replace(keyword, "")
        self.chats.append(to_add)
        self.chats.remove(to_remove)
        coordinator.send_status(ChatUpdates(self, new_chats=[to_add.uid], modified_chats=[self.chat_to_edit.uid], removed_chats=[to_remove.uid]))
        return to_add, self.chat_to_edit, to_remove

    def send_member_update_status(self) -> Tuple[ChatMember, ChatMember, ChatMember]:
        keyword = " (Edited)"
        if self.backup_member not in self.group.members:
            to_add, to_remove = self.backup_member, self.member_to_toggle
            self.member_to_edit.name += keyword
        else:
            to_add, to_remove = self.member_to_toggle, self.backup_member
            self.member_to_edit.name = self.member_to_edit.name.replace(keyword, "")
        self.group.members.append(to_add)
        self.group.members.remove(to_remove)
        coordinator.send_status(MemberUpdates(self, self.group.uid, new_members=[to_add.uid], modified_members=[self.member_to_edit.uid], removed_members=[to_remove.uid]))
        return to_add, self.member_to_edit, to_remove

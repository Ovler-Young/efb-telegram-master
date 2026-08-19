import random
import time
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

from ehforwarderbot import Chat, Message, MsgType, coordinator
from ehforwarderbot.chat import ChatMember, GroupChat, PrivateChat, SystemChat
from ehforwarderbot.message import LinkAttribute, LocationAttribute, MessageCommand, MessageCommands, StatusAttribute, Substitutions
from ehforwarderbot.status import MessageReactionsUpdate
from ehforwarderbot.types import MessageID, ReactionName, Reactions

from .types import extra


class MessageFactoryMixin:
    suggested_reactions: List[ReactionName] = [ReactionName("R0"), ReactionName("R1"), ReactionName("R2"), ReactionName("R3"), ReactionName("R4")]

    @extra(name="Echo", desc="Echo back the input.\nUsage:\n    {function_name} text")
    def echo(self, args):
        return args

    @staticmethod
    def _require_author(author: Optional[ChatMember]) -> ChatMember:
        if author is None:
            raise ValueError("A chat member author is required for mock messages.")
        return author

    def build_reactions(self, group: Chat) -> Reactions:
        possible_reactions = self.suggested_reactions[:-1] + [None]
        reactions: Dict[ReactionName, List[ChatMember]] = {}
        for member in group.members:
            reaction = random.choice(possible_reactions)
            if reaction is None:
                continue
            reactions.setdefault(reaction, []).append(member)
        return reactions

    def send_reactions_update(self, message: Message) -> MessageReactionsUpdate:
        reactions = self.build_reactions(message.chat)
        message.reactions = reactions
        assert message.uid is not None
        status = MessageReactionsUpdate(chat=message.chat, msg_id=message.uid, reactions=reactions)
        coordinator.send_status(status)
        return status

    @staticmethod
    def build_message_commands() -> MessageCommands:
        return MessageCommands([MessageCommand("Ping!", "command_ping"), MessageCommand("Bam", "command_bam")])

    @staticmethod
    def command_ping() -> Optional[str]:
        return "Pong!"

    @staticmethod
    def command_bam():
        return None

    @staticmethod
    def build_substitutions(text: str, chat: Chat) -> Substitutions:
        a_0, a_1, b_0, b_1 = sorted(random.sample(range(len(text) + 1), k=4))
        self_member = chat.self
        assert self_member is not None
        a: Chat | ChatMember = self_member
        if isinstance(chat, GroupChat):
            b = random.choice(chat.members)
        else:
            assert isinstance(chat, (PrivateChat, SystemChat))
            b = chat.other
        if random.randrange(2) == 1:
            a, b = b, a
        return Substitutions({(a_0, a_1): a, (b_0, b_1): b})

    def attach_message_properties(self, message: Message, reactions: bool, commands: bool, substitutions: bool) -> Message:
        message.reactions = self.build_reactions(message.chat) if reactions else {}
        message.commands = self.build_message_commands() if commands else None
        message.substitutions = self.build_substitutions(message.text, message.chat) if substitutions else None
        return message

    def send_text_message(
        self,
        chat: Chat,
        author: Optional[ChatMember] = None,
        target: Optional[Message] = None,
        reactions: bool = False,
        commands: bool = False,
        substitution: bool = False,
        unsupported: bool = False,
        text: Optional[str] = None,
    ) -> Message:
        author = self._require_author(author or chat.self)
        uid = MessageID(f"__msg_id_{uuid4()}__")
        msg_type = MsgType.Unsupported if unsupported else MsgType.Text
        message = Message(chat=chat, author=author, type=msg_type, target=target, uid=uid, text=text or f"Content of {msg_type.name} message with ID {uid}", deliver_to=coordinator.master)
        message = self.attach_message_properties(message, reactions, commands, substitution)
        coordinator.send_message(message)
        self._store_message(message)
        return message

    def edit_text_message(self, message: Message, reactions: bool = False, commands: bool = False, substitution: bool = False) -> Message:
        message.edit = True
        message.text = f"Edited {message.type.name} message {message.uid} @ {time.time_ns()}"
        message = self.attach_message_properties(message, reactions, commands, substitution)
        self._store_message(message)
        coordinator.send_message(message)
        return message

    def send_link_message(
        self, chat: Chat, author: Optional[ChatMember] = None, target: Optional[Message] = None, reactions: bool = False, commands: bool = False, substitution: bool = False
    ) -> Message:
        author = self._require_author(author or chat.self)
        uid = MessageID(f"__msg_id_{uuid4()}__")
        message = Message(
            chat=chat,
            author=author,
            type=MsgType.Link,
            target=target,
            uid=uid,
            text=f"Content of link message with ID {uid}",
            attributes=LinkAttribute(title="EH Forwarder Bot", description="EH Forwarder Bot project site.", url="https://efb.1a23.studio"),
            deliver_to=coordinator.master,
        )
        message = self.attach_message_properties(message, reactions, commands, substitution)
        self._store_message(message)
        coordinator.send_message(message)
        return message

    def edit_link_message(self, message: Message, reactions: bool = False, commands: bool = False, substitution: bool = False) -> Message:
        message.text = f"Content of edited link message with ID {message.uid}"
        message.edit = True
        message.attributes = LinkAttribute(title="EH Forwarder Bot (edited)", description="EH Forwarder Bot project site. (edited)", url="https://efb.1a23.studio/#edited")
        message = self.attach_message_properties(message, reactions, commands, substitution)
        self._store_message(message)
        coordinator.send_message(message)
        return message

    def send_location_message(
        self, chat: Chat, author: Optional[ChatMember] = None, target: Optional[Message] = None, reactions: bool = False, commands: bool = False, substitution: bool = False
    ) -> Message:
        author = self._require_author(author or chat.self)
        uid = MessageID(f"__msg_id_{uuid4()}__")
        message = Message(
            chat=chat,
            author=author,
            type=MsgType.Location,
            target=target,
            uid=uid,
            text=f"Content of location message with ID {uid}",
            attributes=LocationAttribute(latitude=random.uniform(0.0, 90.0), longitude=random.uniform(0.0, 90.0)),
            deliver_to=coordinator.master,
        )
        message = self.attach_message_properties(message, reactions, commands, substitution)
        self._store_message(message)
        coordinator.send_message(message)
        return message

    def edit_location_message(self, message: Message, reactions: bool = False, commands: bool = False, substitution: bool = False) -> Message:
        message.text = f"Content of edited location message with ID {message.uid}"
        message.edit = True
        message.attributes = LocationAttribute(latitude=random.uniform(0.0, 90.0), longitude=random.uniform(0.0, 90.0))
        message = self.attach_message_properties(message, reactions, commands, substitution)
        self._store_message(message)
        coordinator.send_message(message)
        return message

    def send_file_like_message(
        self,
        msg_type: MsgType,
        file_path: Path,
        mime: str,
        chat: Chat,
        author: Optional[ChatMember] = None,
        target: Optional[Message] = None,
        reactions: bool = False,
        commands: bool = False,
        substitution: bool = False,
    ) -> Message:
        author = self._require_author(author or chat.self)
        uid = MessageID(f"__msg_id_{uuid4()}__")
        message = Message(
            chat=chat,
            author=author,
            type=msg_type,
            target=target,
            uid=uid,
            file=file_path.open("rb"),
            filename=file_path.name,
            path=file_path,
            mime=mime,
            text=f"Content of {msg_type.name} message with ID {uid}",
            deliver_to=coordinator.master,
        )
        message = self.attach_message_properties(message, reactions, commands, substitution)
        self._store_message(message)
        coordinator.send_message(message)
        return message

    def edit_file_like_message_text(self, message: Message, reactions: bool = False, commands: bool = False, substitution: bool = False) -> Message:
        message.text = f"Content of edited {message.type.name} message with ID {message.uid}"
        message.edit = True
        message.edit_media = False
        message = self.attach_message_properties(message, reactions, commands, substitution)
        self._store_message(message)
        coordinator.send_message(message)
        return message

    def edit_file_like_message(self, message: Message, file_path: Path, mime: str, reactions: bool = False, commands: bool = False, substitution: bool = False) -> Message:
        message.text = f"Content of edited {message.type.name} media with ID {message.uid}"
        message.edit = True
        message.edit_media = True
        message.file = file_path.open("rb")
        message.filename = file_path.name
        message.path = file_path
        message.mime = mime
        message = self.attach_message_properties(message, reactions, commands, substitution)
        self._store_message(message)
        coordinator.send_message(message)
        return message

    def send_status_message(self, status: StatusAttribute, chat: Chat, author: Optional[ChatMember] = None, target: Optional[Message] = None) -> Message:
        author = self._require_author(author or chat.self)
        uid = MessageID(f"__msg_id_{uuid4()}__")
        message = Message(chat=chat, author=author, type=MsgType.Status, target=target, uid=uid, text="", attributes=status, deliver_to=coordinator.master)
        coordinator.send_message(message)
        self._store_message(message)
        return message

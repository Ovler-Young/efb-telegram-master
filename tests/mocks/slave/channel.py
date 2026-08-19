import threading
from logging import getLogger
from queue import Queue
from typing import Dict, Optional, Set

from ehforwarderbot import Chat, Message, MsgType, Status
from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.chat import ChatMember, GroupChat, PrivateChat
from ehforwarderbot.types import MessageID, ModuleID

from .chats import ChatFixturesMixin
from .delivery import DeliveryMixin
from .messages import MessageFactoryMixin
from .types import ReactionMode


class MockSlaveChannel(ChatFixturesMixin, MessageFactoryMixin, DeliveryMixin, SlaveChannel):
    channel_name: str = "Mock Slave"
    channel_emoji: str = "➖"
    channel_id: ModuleID = ModuleID("tests.mocks.slave")
    supported_message_types: Set[MsgType] = {
        MsgType.Text,
        MsgType.Image,
        MsgType.Voice,
        MsgType.Animation,
        MsgType.Video,
        MsgType.File,
        MsgType.Location,
        MsgType.Link,
        MsgType.Sticker,
        MsgType.Status,
        MsgType.Unsupported,
    }
    __version__: str = "0.0.2"
    logger = getLogger(channel_id)
    CHAT_ID_FORMAT = "__chat_{hash}__"
    polling = threading.Event()

    def __init__(self, instance_id=None):
        super().__init__(instance_id)
        self.generate_chats()
        self.chat_with_alias: PrivateChat = self.chats_by_alias[True][0]
        self.chat_without_alias: PrivateChat = self.chats_by_alias[False][0]
        self.group: GroupChat = self.chats_by_chat_type["GroupChat"][0]
        self.messages: "Queue[Message]" = Queue()
        self.statuses: "Queue[Status]" = Queue()
        self.messages_sent: Dict[MessageID, Message] = {}
        self.message_removal_possible: bool = True
        self.accept_message_reactions: ReactionMode = "accept"
        self.chat_to_toggle: PrivateChat = self.get_chat(self.CHAT_ID_FORMAT.format(hash=hash("I")))
        self.chat_to_edit: PrivateChat = self.get_chat(self.CHAT_ID_FORMAT.format(hash=hash("われ")))
        self.member_to_toggle: ChatMember = self.get_chat(self.group.uid).get_member(self.CHAT_ID_FORMAT.format(hash=hash("Ю")))
        self.member_to_edit: ChatMember = self.get_chat(self.group.uid).get_member(self.CHAT_ID_FORMAT.format(hash=hash("Я")))

    def get_message_by_id(self, chat: "Chat", msg_id: MessageID) -> Optional["Message"]:
        raise NotImplementedError

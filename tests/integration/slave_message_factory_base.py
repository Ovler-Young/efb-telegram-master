from abc import ABC, abstractmethod
from itertools import chain
from typing import List, Optional, Tuple

from ehforwarderbot import Chat
from ehforwarderbot import Message as EFBMessage
from ehforwarderbot.chat import SelfChatMember
from telethon.tl.custom import Message
from telethon.tl.types import MessageEntityCode, MessageEntityMentionName

from tests.mocks.slave.channel import MockSlaveChannel


class MessageFactory(ABC):
    """Interface of factory to generate messages."""

    content_editable = True
    """If the message content is editable in Telegram."""

    media_editable = True
    """If the message media is editable in Telegram."""

    @abstractmethod
    def send_message(self, slave: MockSlaveChannel, chat: Chat, target: Optional[Message] = None) -> Message:
        """Build an initial message to send with."""

    @abstractmethod
    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        """Compare if the Telegram message matches with what is processed by ETM.

        This method should raises ``AssertionError`` if a mismatch is found.
        Otherwise this shall return nothing (i.e. ``None``).
        """

    def edit_message(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        """Issue an edit of the message if applicable.

        Returns the edited message, or none if no edit is needed."""
        return None

    def edit_message_media(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        """Issue a media edit of the message if applicable.

        Returns the edited message, or none if no edit is needed."""
        return None

    def finalize_message(self, efb_msg: EFBMessage):
        """Finalize the message before discarding if needed."""
        pass

    @staticmethod
    def compare_substitutions(tg_msg: Message, efb_msg: EFBMessage) -> None:
        """Compare application of substitution in message text."""
        if not efb_msg.substitutions:
            return
        self_subs: List[Tuple[MessageEntityMentionName, str]] = tg_msg.get_entities_text(cls=MessageEntityMentionName)
        other_subs: List[Tuple[MessageEntityCode, str]] = tg_msg.get_entities_text(cls=MessageEntityCode)
        for coord, chat in efb_msg.substitutions.items():
            size = coord[1] - coord[0]
            if isinstance(chat, SelfChatMember):
                assert any(ent.length == size for ent, _ in self_subs), f"string of size {size} is not found in self_subs: {[(x.to_dict(), y) for x, y in self_subs]}"
            else:
                assert any(ent.length == size for ent, _ in other_subs), f"string of size {size} is not found in other_subs: {[(x.to_dict(), y) for x, y in other_subs]}"

    @staticmethod
    def assert_metadata_in_buttons(tg_msg: Message, efb_msg: EFBMessage):
        """Compare metadata (text, reactions and commands) in the case
        when sent in buttons.
        """
        assert any(efb_msg.text in btn.text for btn in chain.from_iterable(tg_msg.buttons))
        for r_name in efb_msg.reactions:
            assert any(r_name in btn.text for btn in chain.from_iterable(tg_msg.buttons))
        if efb_msg.commands:
            assert tg_msg.button_count >= len(efb_msg.commands)

    def __str__(self):
        return self.__class__.__name__

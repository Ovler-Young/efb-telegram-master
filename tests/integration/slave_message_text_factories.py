from typing import Optional

from ehforwarderbot import Chat
from ehforwarderbot import Message as EFBMessage
from ehforwarderbot.message import LinkAttribute, LocationAttribute
from pytest import approx
from telethon.tl.custom import Message

from tests.integration.slave_message_factory_base import MessageFactory
from tests.mocks.slave.channel import MockSlaveChannel


class TextMessageFactory(MessageFactory):
    def __init__(self, unsupported=False):
        self.unsupported = unsupported

    def send_message(self, slave: MockSlaveChannel, chat: Chat, target: Optional[Message] = None) -> Message:
        return slave.send_text_message(chat, target=target, reactions=True, commands=True, substitution=True, unsupported=self.unsupported)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert efb_msg.text in tg_msg.raw_text
        if self.unsupported:
            assert "unsupported" in tg_msg.raw_text.lower()
        for i in efb_msg.reactions:
            assert i in tg_msg.raw_text
        if efb_msg.commands:
            assert tg_msg.button_count == len(efb_msg.commands)
        self.compare_substitutions(tg_msg, efb_msg)

    def edit_message(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_text_message(message, reactions=True, commands=True, substitution=True)

    def __str__(self):
        if self.unsupported:
            return "UnsupportedMessage"
        return super().__str__()


class LinkMessageFactory(MessageFactory):
    def send_message(self, slave: MockSlaveChannel, chat: Chat, target: Optional[Message] = None) -> Message:
        return slave.send_link_message(chat, target=target, reactions=True, commands=True, substitution=True)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert efb_msg.text in tg_msg.raw_text
        for i in efb_msg.reactions:
            assert i in tg_msg.raw_text
        if efb_msg.commands:
            assert tg_msg.button_count == len(efb_msg.commands)
        assert isinstance(efb_msg.attributes, LinkAttribute)
        if efb_msg.attributes.title:
            assert efb_msg.attributes.title in tg_msg.raw_text
        if efb_msg.attributes.description:
            assert efb_msg.attributes.description in tg_msg.raw_text
        if efb_msg.attributes.image:
            assert efb_msg.attributes.image in tg_msg.text
        if efb_msg.attributes.url:
            assert efb_msg.attributes.url in tg_msg.text
        self.compare_substitutions(tg_msg, efb_msg)

    def edit_message(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_link_message(message, reactions=True, commands=True, substitution=True)


class LocationMessageFactory(MessageFactory):
    content_editable = False

    def send_message(self, slave: MockSlaveChannel, chat: Chat, target: Optional[Message] = None) -> Message:
        return slave.send_location_message(chat, target=target, reactions=True, commands=True, substitution=True)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        self.assert_metadata_in_buttons(tg_msg, efb_msg)
        assert isinstance(efb_msg.attributes, LocationAttribute)
        assert tg_msg.geo
        assert efb_msg.attributes.latitude == approx(tg_msg.geo.lat, abs=1e-3)
        assert efb_msg.attributes.longitude == approx(tg_msg.geo.long, abs=1e-3)

    def edit_message(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_location_message(message, reactions=True, commands=True, substitution=True)

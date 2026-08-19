from typing import Optional
from uuid import uuid4

from ehforwarderbot import Message as EFBMessage
from ehforwarderbot import MsgType
from ehforwarderbot.message import LocationAttribute
from pytest import approx
from telethon import TelegramClient
from telethon.tl.custom import Message
from telethon.tl.types import InputGeoPoint, InputMediaContact, InputMediaDice, InputMediaGeoPoint, InputMediaVenue, MessageMediaDice, MessageMediaVenue

from .master_message_factory_base import MessageFactory


class TextMessageFactory(MessageFactory):
    async def send_message(self, client: TelegramClient, chat_id: int, target: Message = None) -> Message:
        return await client.send_message(chat_id, f"守ったものは、明るい未来幻想を見せながら消えてゆくヒカリ。\nnew message {uuid4()}, target: {target and target.id}", reply_to=target)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert efb_msg.type == MsgType.Text
        assert tg_msg.text == efb_msg.text

    async def edit_message(self, client: TelegramClient, message: Message) -> Optional[Message]:
        return await message.edit(
            text=f"信じたものは、都合のいい妄想を繰り返し映し出す鏡。\nedited message {uuid4()}",
        )


class LocationMessageFactory(MessageFactory):
    async def send_message(self, client: TelegramClient, chat_id: int, target: Message = None) -> Message:
        return await client.send_message(chat_id, file=InputMediaGeoPoint(InputGeoPoint(0.0, 0.0)), reply_to=target)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert efb_msg.type == MsgType.Location
        assert isinstance(efb_msg.attributes, LocationAttribute)
        assert tg_msg.geo.lat == approx(efb_msg.attributes.latitude, abs=1e-3)
        assert tg_msg.geo.long == approx(efb_msg.attributes.longitude, abs=1e-3)


class VenueMessageFactory(MessageFactory):
    async def send_message(self, client: TelegramClient, chat_id: int, target: Message = None) -> Message:
        return await client.send_message(chat_id, file=InputMediaVenue(InputGeoPoint(0.0, 0.0), "Location name", f"Address {uuid4()}", "", "", ""), reply_to=target)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert efb_msg.type == MsgType.Location
        assert isinstance(efb_msg.attributes, LocationAttribute)
        assert tg_msg.geo.lat == approx(efb_msg.attributes.latitude, abs=1e-3)
        assert tg_msg.geo.long == approx(efb_msg.attributes.longitude, abs=1e-3)
        assert isinstance(tg_msg.media, MessageMediaVenue)
        assert tg_msg.media.title in efb_msg.text
        assert tg_msg.media.address in efb_msg.text


class ContactMessageFactory(MessageFactory):
    async def send_message(self, client: TelegramClient, chat_id: int, target: Message = None) -> Message:
        return await client.send_message(chat_id, file=InputMediaContact("+424 3 14159", "Bot", "Support", ""), reply_to=target)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert efb_msg.type == MsgType.Text
        assert tg_msg.contact
        assert tg_msg.contact.phone_number in efb_msg.text
        assert tg_msg.contact.first_name in efb_msg.text
        assert tg_msg.contact.last_name in efb_msg.text


class DiceMessageFactory(MessageFactory):
    def __init__(self, emoji: str):
        self.emoji = emoji

    async def send_message(self, client: TelegramClient, chat_id: int, target: Message = None) -> Message:
        return await client.send_message(chat_id, f"Dice caption {uuid4()}", file=InputMediaDice(self.emoji), reply_to=target)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert efb_msg.type == MsgType.Text
        media = tg_msg.media
        assert isinstance(media, MessageMediaDice)
        assert str(media.emoticon) in efb_msg.text
        assert str(media.value) in efb_msg.text

    def __str__(self):
        return f"DiceMessageFactory({self.emoji})"

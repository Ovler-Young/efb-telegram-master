from typing import BinaryIO, Dict, List, Optional

from ehforwarderbot import Chat
from ehforwarderbot.chat import ChatMember, ChatNotificationState, GroupChat, PrivateChat, SystemChat
from ehforwarderbot.exceptions import EFBChatNotFound, EFBOperationNotSupported
from ehforwarderbot.types import ChatID

from .types import ChatTypeName


class ChatFixturesMixin:
    __picture_dict: Dict[str, str] = {}

    __chat_templates = [
        ("A", PrivateChat, ChatNotificationState.NONE, "A.png", "Alice"),
        ("B", PrivateChat, ChatNotificationState.MENTIONS, "B.png", "Bob"),
        ("C", PrivateChat, ChatNotificationState.ALL, "C.png", "Carol"),
        ("D", SystemChat, ChatNotificationState.NONE, "D.png", "Dave"),
        ("E", SystemChat, ChatNotificationState.MENTIONS, "E.png", "Eve"),
        ("F", SystemChat, ChatNotificationState.ALL, "F.png", "Frank"),
        ("G", PrivateChat, ChatNotificationState.NONE, "G.png", None),
        ("H", PrivateChat, ChatNotificationState.MENTIONS, "H.png", None),
        ("I", PrivateChat, ChatNotificationState.ALL, "I.png", None),
        ("J", SystemChat, ChatNotificationState.NONE, "J.png", None),
        ("K", SystemChat, ChatNotificationState.MENTIONS, "K.png", None),
        ("L", SystemChat, ChatNotificationState.ALL, "L.png", None),
        ("Ur", GroupChat, ChatNotificationState.NONE, "U.png", "Uranus"),
        ("Ve", GroupChat, ChatNotificationState.MENTIONS, "V.png", "Venus"),
        ("Wo", GroupChat, ChatNotificationState.ALL, "W.png", "Wonderland"),
        ("Xe", GroupChat, ChatNotificationState.NONE, "X.png", None),
        ("Yb", GroupChat, ChatNotificationState.MENTIONS, "Y.png", None),
        ("Zn", GroupChat, ChatNotificationState.ALL, "Z.png", None),
        ("あ", PrivateChat, ChatNotificationState.NONE, None, "あべ"),
        ("い", PrivateChat, ChatNotificationState.MENTIONS, None, "いとう"),
        ("う", PrivateChat, ChatNotificationState.ALL, None, "うえだ"),
        ("え", SystemChat, ChatNotificationState.NONE, None, "えのもと"),
        ("お", SystemChat, ChatNotificationState.MENTIONS, None, "おがわ"),
        ("か", SystemChat, ChatNotificationState.ALL, None, "かとう"),
        ("き", PrivateChat, ChatNotificationState.NONE, None, None),
        ("く", PrivateChat, ChatNotificationState.MENTIONS, None, None),
        ("け", PrivateChat, ChatNotificationState.ALL, None, None),
        ("こ", SystemChat, ChatNotificationState.NONE, None, None),
        ("さ", SystemChat, ChatNotificationState.MENTIONS, None, None),
        ("し", SystemChat, ChatNotificationState.ALL, None, None),
        ("らん", GroupChat, ChatNotificationState.NONE, None, "ランド"),
        ("りぞ", GroupChat, ChatNotificationState.MENTIONS, None, "リゾート"),
        ("るう", GroupChat, ChatNotificationState.ALL, None, "ルートディレクトリ"),
        ("れつ", GroupChat, ChatNotificationState.NONE, None, None),
        ("ろく", GroupChat, ChatNotificationState.MENTIONS, None, None),
        ("われ", GroupChat, ChatNotificationState.ALL, None, None),
    ]

    __group_member_templates = [
        ("A", ChatNotificationState.NONE, "A.png", "安"),
        ("B & S", ChatNotificationState.MENTIONS, "B.png", "柏"),
        ("C", ChatNotificationState.ALL, "C.png", "陈"),
        ("D", ChatNotificationState.NONE, "D.png", None),
        ("E", ChatNotificationState.MENTIONS, "E.png", None),
        ("F", ChatNotificationState.ALL, "F.png", None),
        ("Ал", ChatNotificationState.NONE, None, "Александра"),
        ("Бэ", ChatNotificationState.MENTIONS, None, "Борис"),
        ("Вэ", ChatNotificationState.ALL, None, "Владислав"),
        ("Э", ChatNotificationState.NONE, None, None),
        ("Ю", ChatNotificationState.MENTIONS, None, None),
        ("Я", ChatNotificationState.ALL, None, None),
    ]

    def generate_chats(self):
        self.chats: List[Chat] = []
        self.chats_by_chat_type: Dict[ChatTypeName, List[Chat]] = {"PrivateChat": [], "GroupChat": [], "SystemChat": []}
        self.chats_by_notification_state: Dict[ChatNotificationState, List[Chat]] = {
            ChatNotificationState.ALL: [],
            ChatNotificationState.MENTIONS: [],
            ChatNotificationState.NONE: [],
        }
        self.chats_by_profile_picture: Dict[bool, List[Chat]] = {True: [], False: []}
        self.chats_by_alias: Dict[bool, List[Chat]] = {True: [], False: []}

        for name, chat_type, notification, avatar, alias in self.__chat_templates:
            chat = chat_type(channel=self, name=name, alias=alias, uid=ChatID(self.CHAT_ID_FORMAT.format(hash=hash(name))), notification=notification)
            self.__picture_dict[chat.uid] = avatar
            if chat_type == GroupChat:
                self.fill_group(chat)
            self.chats_by_chat_type[chat_type.__name__].append(chat)
            self.chats_by_notification_state[notification].append(chat)
            self.chats_by_profile_picture[avatar is not None].append(chat)
            self.chats_by_alias[alias is not None].append(chat)
            self.chats.append(chat)

        name = "Unknown Chat"
        self.unknown_chat: PrivateChat = PrivateChat(channel=self, name=name, alias="不知道", uid=ChatID(self.CHAT_ID_FORMAT.format(hash=hash(name))), notification=ChatNotificationState.ALL)
        name = "Unknown Chat @ unknown channel"
        self.unknown_channel: PrivateChat = PrivateChat(
            module_id="__this_is_not_a_channel__",
            module_name="Unknown Channel",
            channel_emoji="‼️",
            name=name,
            alias="知らんでぇ",
            uid=ChatID(self.CHAT_ID_FORMAT.format(hash=hash(name))),
            notification=ChatNotificationState.ALL,
        )
        name = "backup_chat"
        self.backup_chat: PrivateChat = PrivateChat(channel=self, name=name, uid=ChatID(self.CHAT_ID_FORMAT.format(hash=hash(name))), notification=ChatNotificationState.ALL)
        name = "backup_member"
        self.backup_member: ChatMember = ChatMember(self.chats_by_chat_type["GroupChat"][0], name=name, uid=ChatID(self.CHAT_ID_FORMAT.format(hash=hash(name))))

    def fill_group(self, group: Chat):
        for name, notification, avatar, alias in self.__group_member_templates:
            group.add_member(name=name, alias=f"{alias} @ {group.name[::-1]}" if alias is not None else None, uid=ChatID(self.CHAT_ID_FORMAT.format(hash=hash(name))))

    def get_chat(self, chat_uid: str) -> Chat:
        for chat in self.chats:
            if chat_uid == chat.uid:
                return chat
        raise EFBChatNotFound()

    def get_chats(self) -> List[Chat]:
        return self.chats.copy()

    def get_chat_picture(self, chat: Chat) -> BinaryIO:
        avatar = self.__picture_dict.get(chat.uid)
        if avatar:
            return open(f"tests/mocks/{avatar}", "rb")
        raise EFBOperationNotSupported("This chat has no profile picture.")

    def get_chats_by_criteria(
        self, chat_type: Optional[ChatTypeName] = None, notification: Optional[ChatNotificationState] = None, avatar: Optional[bool] = None, alias: Optional[bool] = None
    ) -> List[Chat]:
        chats = self.chats.copy()
        if chat_type is not None:
            chats = [chat for chat in chats if chat in self.chats_by_chat_type[chat_type]]
        if notification is not None:
            chats = [chat for chat in chats if chat in self.chats_by_notification_state[notification]]
        if avatar is not None:
            chats = [chat for chat in chats if chat in self.chats_by_profile_picture[avatar]]
        if alias is not None:
            chats = [chat for chat in chats if chat in self.chats_by_alias[alias]]
        return chats

    def get_chat_by_criteria(self, chat_type: Optional[ChatTypeName] = None, notification: Optional[ChatNotificationState] = None, avatar: Optional[bool] = None, alias: Optional[bool] = None) -> Chat:
        return self.get_chats_by_criteria(chat_type=chat_type, notification=notification, avatar=avatar, alias=alias)[0]

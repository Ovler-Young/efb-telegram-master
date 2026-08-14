"""Reconstruct EFB messages from persisted MsgLog records."""

from __future__ import annotations

import pickle
from contextlib import suppress
from typing import Callable, Dict, List, Optional

from ehforwarderbot import Channel, MsgType
from ehforwarderbot.message import Substitutions
from ehforwarderbot.types import MessageID, ModuleID, ReactionName

from .chat_member import ETMChatMember
from .chat_object_cache import ChatObjectCacheManager
from .message import ETMMsg
from .models import MsgLog, PickledDict
from .msg_type import TGMsgType
from .utils import EFBChannelChatIDStr, TgChatMsgIDStr, chat_id_str_to_id


class MsgLogReconstructor:
    """Build domain messages using explicitly supplied persistence and cache services."""

    def __init__(
        self,
        get_msg_log: Callable[..., Optional[MsgLog]],
        chat_manager: ChatObjectCacheManager,
        get_module_by_id: Callable[[ModuleID], object],
    ) -> None:
        self.get_msg_log = get_msg_log
        self.chat_manager = chat_manager
        self.get_module_by_id = get_module_by_id

    def build(self, row: MsgLog, recur: bool = True) -> ETMMsg:
        c_module, c_id, _ = chat_id_str_to_id(EFBChannelChatIDStr(row.slave_origin_uid))
        assert row.slave_member_uid is not None
        a_module, a_id, a_grp = chat_id_str_to_id(EFBChannelChatIDStr(row.slave_member_uid))
        chat = self.chat_manager.get_chat(c_module, c_id, build_dummy=True)
        author = self.chat_manager.get_chat_member(a_module, a_grp, a_id, build_dummy=True)
        msg = ETMMsg(
            uid=MessageID(row.slave_message_id),
            chat=chat,
            author=author,
            text=row.text,
            type=MsgType(row.msg_type),
            type_telegram=TGMsgType(row.media_type),
            mime=row.mime or None,
            file_id=row.file_id or None,
        )
        msg.sender_bot_id = row.sender_bot_id
        with suppress(NameError):
            to_module = self.get_module_by_id(ModuleID(row.sent_to))
            if isinstance(to_module, Channel):
                msg.deliver_to = to_module
        if row.pickle:
            pickle_data = bytes(row.pickle) if isinstance(row.pickle, memoryview) else row.pickle
            misc_data: PickledDict = pickle.loads(pickle_data)
            if "target" in misc_data and recur:
                target_row = self.get_msg_log(master_msg_id=TgChatMsgIDStr(misc_data["target"]))
                if target_row:
                    msg.target = self.build(target_row, recur=False)
            if "is_system" in misc_data:
                msg.is_system = misc_data["is_system"]
            if "attributes" in misc_data:
                msg.attributes = misc_data["attributes"]
            if "commands" in misc_data:
                msg.commands = misc_data["commands"]
            if "substitutions" in misc_data:
                substitutions = Substitutions({})
                for key, value in misc_data["substitutions"].items():
                    module_id, chat_id, group_id = chat_id_str_to_id(value)
                    if group_id:
                        substitutions[key] = self.chat_manager.get_chat_member(module_id, group_id, chat_id, build_dummy=True)
                    else:
                        substitutions[key] = self.chat_manager.get_chat(module_id, chat_id, build_dummy=True)
                msg.substitutions = substitutions
            if "reactions" in misc_data:
                reactions: Dict[ReactionName, List[ETMChatMember]] = {}
                for reaction, reactors in misc_data["reactions"].items():
                    reactions[reaction] = []
                    for reactor in reactors:
                        module_id, chat_id, group_id = chat_id_str_to_id(reactor)
                        reactions[reaction].append(self.chat_manager.get_chat_member(module_id, group_id, chat_id, build_dummy=True))
                msg.reactions = reactions
        return msg

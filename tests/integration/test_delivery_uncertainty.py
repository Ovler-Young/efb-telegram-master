"""Real Telegram acceptance followed by injected loss of the response."""
import asyncio
import uuid

import httpx
import pytest
from telegram import Bot
from telegram.error import TimedOut

from ehforwarderbot import MsgType
from efb_telegram_master.outbound import DeliveryUncertainError, QueueRequest
from efb_telegram_master.bot_manager import QueuedDbLogContext
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.msg_type import TGMsgType

pytestmark = pytest.mark.asyncio


async def test_remote_accepted_document_is_not_resent_after_response_loss(channel, bot_group, client, slave, monkeypatch):
    manager = channel.bot_manager
    filename = "retry-safety-" + uuid.uuid4().hex + ".txt"
    accepted = []
    original_send = Bot.send_document

    async def accept_then_lose_response(bot, *args, **kwargs):
        result = await original_send(bot, *args, **kwargs)
        if kwargs.get("filename") == filename:
            accepted.append(result)
            try:
                raise httpx.ReadTimeout("CI-injected response loss after actual Telegram acceptance")
            except httpx.ReadTimeout as cause:
                raise TimedOut("CI response loss") from cause
        return result

    monkeypatch.setattr(Bot, "send_document", accept_then_lose_response)
    source_message = ETMMsg(
        uid=filename, text="EFB duplicate-send regression test", chat=slave.chat_with_alias,
        author=slave.chat_with_alias.other, type=MsgType.File, type_telegram=TGMsgType.Document,
        deliver_to=channel,
    )
    row_id, waiter = manager._enqueue_requests([
        QueueRequest("send_document", (int(bot_group), b"EFB duplicate-send regression test\n"), {"filename": filename})
    ], db_log_context=QueuedDbLogContext(source_message))
    try:
        with pytest.raises(DeliveryUncertainError):
            await asyncio.wait_for(asyncio.wrap_future(waiter), timeout=60)
        # Longer than the former one-second blind retry delay.
        await asyncio.sleep(2.1)
        assert len(accepted) == 1
        remote = await client.get_messages(bot_group, ids=accepted[0].message_id)
        assert remote is not None and remote.file.name == filename
        assert manager.confirm_queued_delivery(int(row_id), accepted[0])
        stored = channel.db.get_msg_log(master_msg_id=f"{bot_group}.{accepted[0].message_id}")
        assert stored is not None and stored.slave_message_id == filename
        assert stored.file_id == accepted[0].document.file_id
        with manager._outbound_queue._lock:
            assert manager._outbound_queue.connection.execute(
                "SELECT id FROM outbound_queue WHERE id=?", (int(row_id),)
            ).fetchone() is None
        assert len(accepted) == 1
    finally:
        # Remove only this test's known messages, never unrelated chat history.
        if accepted:
            await client.delete_messages(bot_group, [message.message_id for message in accepted])

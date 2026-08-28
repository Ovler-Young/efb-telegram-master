import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from peewee import SqliteDatabase
from telegram import Chat, Message, Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.db import MsgLog, SYNTHETIC_MSGLOG_PREFIX, TopicAssoc, database
from efb_telegram_master.msglog_backfill import (
    BackfillRow,
    MsgLogBackfillStore,
    MsgLogGap,
    MsgLogGapBackfiller,
    TelethonHistorySource,
)


@pytest.fixture
def msglog_database():
    sqlite = SqliteDatabase(":memory:")
    database.initialize(sqlite)
    sqlite.connect()
    sqlite.create_tables([MsgLog, TopicAssoc])
    yield sqlite
    sqlite.close()


def _log(message_id: int, *, chat_id: int = -100) -> None:
    MsgLog.create(
        master_msg_id=f"{chat_id}.{message_id}",
        slave_message_id=f"source-{message_id}",
        text="existing",
        slave_origin_uid="tests.mocks.slave chat",
        slave_member_uid="tests.mocks.slave __self__",
        media_type="Text",
        msg_type="Text",
        sent_to="blueset.telegram",
    )


def _message(
    message_id: int,
    sender_id: int,
    *,
    topic_id: int = 9,
    text: str = "content",
    bot: bool = True,
):
    return SimpleNamespace(
        id=message_id,
        sender_id=sender_id,
        sender=SimpleNamespace(id=sender_id, bot=bot),
        reply_to=SimpleNamespace(reply_to_top_id=topic_id),
        message=text,
        media=None,
        action=None,
        date=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
    )


class _History:
    def __init__(self, messages_by_gap, *, fail_at=None, anchors_by_gap=None):
        self.messages_by_gap = messages_by_gap
        self.fail_at = fail_at
        self.anchors_by_gap = anchors_by_gap or {}
        self.started = []

    async def get_anchors(self, gap):
        self.started.append(("anchors", gap))
        return self.anchors_by_gap.get(
            gap, (SimpleNamespace(id=gap.left), SimpleNamespace(id=gap.right))
        )

    async def iter_gap(self, gap):
        self.started.append(("gap", gap))
        if gap == self.fail_at:
            raise RuntimeError("history failed")
        for message in self.messages_by_gap.get(gap, ()):
            yield message


def test_gap_discovery_uses_strict_threshold_and_numeric_chat_order(msglog_database):
    for message_id in (1, 22, 23):
        _log(message_id, chat_id=-10)
    for message_id in (1, 23):
        _log(message_id, chat_id=-20)
    MsgLog.create(
        master_msg_id="invalid",
        slave_message_id="invalid",
        text="",
        slave_origin_uid="tests.mocks.slave chat",
        slave_member_uid="tests.mocks.slave __self__",
        media_type="Text",
        msg_type="Text",
        sent_to="blueset.telegram",
    )

    assert MsgLogBackfillStore().find_gaps() == [MsgLogGap(-20, 1, 23)]


@pytest.mark.asyncio
async def test_telethon_history_uses_exclusive_anchors_and_ascending_iteration():
    calls = []

    class Client:
        async def get_messages(self, chat_id, **kwargs):
            calls.append(("anchors", chat_id, kwargs))
            return [SimpleNamespace(id=message_id) for message_id in kwargs["ids"]]

        def iter_messages(self, chat_id, **kwargs):
            calls.append(("gap", chat_id, kwargs))

            async def messages():
                for message_id in (11, 12):
                    yield SimpleNamespace(id=message_id)

            return messages()

    gap = MsgLogGap(-100, 10, 13)
    source = TelethonHistorySource(Client())
    anchors = await source.get_anchors(gap)
    received = [message.id async for message in source.iter_gap(gap)]

    assert [message.id for message in anchors] == [10, 13]
    assert received == [11, 12]
    assert calls == [
        ("anchors", -100, {"ids": [10, 13]}),
        ("gap", -100, {"min_id": 10, "max_id": 13, "reverse": True}),
    ]


@pytest.mark.asyncio
async def test_backfill_is_serial_stops_on_failure_and_preserves_sender_identity(msglog_database):
    for message_id in (1, 23, 50):
        _log(message_id)
    TopicAssoc.create(topic_chat_id="-100", message_thread_id="9", slave_uid="tests.mocks.slave chat")
    first = MsgLogGap(-100, 1, 23)
    second = MsgLogGap(-100, 23, 50)
    history = _History({
        first: [_message(2, 1000), _message(3, 2000), _message(4, 3000, bot=False)]
    }, fail_at=second)

    with pytest.raises(RuntimeError, match="history failed"):
        await MsgLogGapBackfiller(MsgLogBackfillStore(), history, main_bot_id=1000).run()

    assert history.started == [
        ("anchors", first), ("gap", first), ("anchors", second), ("gap", second)
    ]
    rows = list(MsgLog.select().where(
        MsgLog.master_msg_id.in_(["-100.2", "-100.3", "-100.4"])
    ).order_by(MsgLog.master_msg_id))
    assert [(row.master_msg_id, row.sender_bot_id, row.slave_origin_uid) for row in rows] == [
        ("-100.2", None, "tests.mocks.slave chat"),
        ("-100.3", "2000", "tests.mocks.slave chat"),
    ]


def _row(message_id: int, text: str = "new") -> BackfillRow:
    return BackfillRow(
        master_msg_id=f"-100.{message_id}",
        slave_message_id=f"{SYNTHETIC_MSGLOG_PREFIX}-100.{message_id}",
        text=text,
        slave_origin_uid="tests.mocks.slave chat",
        slave_member_uid="tests.mocks.slave __self__",
        media_type="Text",
        mime=None,
        msg_type="Text",
        sent_to="blueset.telegram",
        sender_bot_id=None,
        time=None,
    )


def test_gap_insert_rolls_back_all_chunks_and_does_not_overwrite(msglog_database):
    class FailingStore(MsgLogBackfillStore):
        INSERT_CHUNK_SIZE = 1

        def __init__(self):
            self.calls = 0

        def _insert_batch(self, rows):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("write failed")
            return super()._insert_batch(rows)

    with pytest.raises(RuntimeError, match="write failed"):
        FailingStore().insert_gap([_row(2), _row(3)])
    assert MsgLog.select().where(MsgLog.master_msg_id.in_(["-100.2", "-100.3"])).count() == 0

    _log(2)
    assert MsgLogBackfillStore().insert_gap([_row(2, "replacement"), _row(3)]) == 1
    assert MsgLog.get_by_id("-100.2").text == "existing"
    assert MsgLog.get_by_id("-100.3").text == "new"


def test_alternate_id_is_not_a_gap_anchor_or_overwritten(msglog_database):
    _log(1)
    _log(50)
    MsgLog.update(master_msg_id_alt="-100.23").where(MsgLog.master_msg_id == "-100.50").execute()

    assert MsgLogBackfillStore().find_gaps() == [MsgLogGap(-100, 1, 50)]
    assert MsgLogBackfillStore().insert_gap([_row(23), _row(24)]) == 1
    assert MsgLog.get_by_id("-100.50").master_msg_id_alt == "-100.23"
    assert MsgLog.get_or_none(MsgLog.master_msg_id == "-100.23") is None
    assert MsgLog.get_by_id("-100.24").text == "new"


@pytest.mark.asyncio
async def test_unmapped_and_service_messages_are_skipped_as_one_completed_gap(msglog_database):
    for message_id in (1, 23):
        _log(message_id)
    TopicAssoc.create(topic_chat_id="-100", message_thread_id="9", slave_uid="tests.mocks.slave chat")
    gap = MsgLogGap(-100, 1, 23)
    service = _message(3, 1000)
    service.action = object()
    history = _History({gap: [_message(2, 1000, topic_id=99), service, _message(4, 1000)]})

    results = await MsgLogGapBackfiller(MsgLogBackfillStore(), history, main_bot_id=1000).run()

    assert results[0].inserted == 1
    assert results[0].skipped == {"unmapped": 1, "service": 1, "deleted": 18}
    assert MsgLog.get_by_id("-100.4").slave_origin_uid == "tests.mocks.slave chat"


@pytest.mark.asyncio
async def test_invisible_anchor_stops_before_writes_or_next_gap(msglog_database):
    for message_id in (1, 23, 50):
        _log(message_id)
    TopicAssoc.create(topic_chat_id="-100", message_thread_id="9", slave_uid="tests.mocks.slave chat")
    first = MsgLogGap(-100, 1, 23)
    second = MsgLogGap(-100, 23, 50)
    history = _History(
        {first: [_message(2, 1000)], second: [_message(24, 1000)]},
        anchors_by_gap={first: (SimpleNamespace(id=1),)},
    )

    with pytest.raises(ValueError, match="not both visible"):
        await MsgLogGapBackfiller(MsgLogBackfillStore(), history, main_bot_id=1000).run()

    assert history.started == [("anchors", first)]
    assert MsgLog.get_or_none(MsgLog.master_msg_id == "-100.2") is None


def test_react_rejects_synthetic_msglog_identity():
    chat = Chat(-100, "supergroup")
    target = Message(2, datetime.datetime.now(datetime.timezone.utc), chat, text="target")
    command = Message(
        3,
        datetime.datetime.now(datetime.timezone.utc),
        chat,
        text="/react value",
        reply_to_message=target,
    )
    channel = object.__new__(TelegramChannel)
    channel.db = SimpleNamespace(
        get_msg_log=Mock(return_value=SimpleNamespace(
            slave_message_id=f"{SYNTHETIC_MSGLOG_PREFIX}-100.2"
        ))
    )
    channel.bot_manager = Mock()

    with patch("efb_telegram_master.sync_reply_text") as reply, \
            patch("efb_telegram_master.coordinator.send_status") as send_status:
        TelegramChannel.react(channel, Update(1, message=command), Mock())

    reply.assert_called_once()
    send_status.assert_not_called()

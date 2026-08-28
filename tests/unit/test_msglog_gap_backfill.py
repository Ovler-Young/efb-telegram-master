import datetime
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from peewee import SqliteDatabase
from telegram import Chat, Message, Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.db import (
    MsgLog,
    MsgLogBackfillCheckpoint,
    SYNTHETIC_MSGLOG_PREFIX,
    TopicAssoc,
    database,
)
from efb_telegram_master.msglog_backfill import (
    BackfillRow,
    GapResult,
    MsgLogBackfillStore,
    MsgLogGap,
    MsgLogGapBackfiller,
    PendingGap,
    TelethonHistorySource,
)
import efb_telegram_master.msglog_backfill as msglog_backfill


@pytest.fixture
def msglog_database():
    sqlite = SqliteDatabase(":memory:")
    database.initialize(sqlite)
    sqlite.connect()
    sqlite.create_tables([MsgLog, MsgLogBackfillCheckpoint, TopicAssoc])
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
        date=datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc),
    )


class _History:
    def __init__(self, messages_by_gap, *, fail_at=None):
        self.messages_by_gap = messages_by_gap
        self.fail_at = fail_at
        self.started = []

    async def iter_gap(self, gap):
        self.started.append(gap)
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


def test_gap_discovery_uses_id_one_as_the_virtual_leading_bound(msglog_database):
    _log(23)

    assert MsgLogBackfillStore().find_gaps() == [MsgLogGap(-100, 0, 23)]


@pytest.mark.asyncio
async def test_backfill_leading_gap_includes_message_id_one(msglog_database):
    _log(22)
    TopicAssoc.create(topic_chat_id="-100", message_thread_id="9", slave_uid="tests.mocks.slave chat")
    gap = MsgLogGap(-100, 0, 22)
    history = _History({gap: [_message(1, 1000)]})

    await MsgLogGapBackfiller(MsgLogBackfillStore(), history, main_bot_id=1000).run()

    assert history.started == [gap]
    assert MsgLog.get_by_id("-100.1").text == "content"


@pytest.mark.asyncio
async def test_telethon_history_uses_cutoff_probe_before_ascending_interval():
    calls = []

    class Client:
        def iter_messages(self, chat_id, **kwargs):
            calls.append((chat_id, kwargs))

            async def messages():
                message_ids = (10,) if "offset_date" in kwargs else (11, 12)
                for message_id in message_ids:
                    yield SimpleNamespace(id=message_id)

            return messages()

    gap = MsgLogGap(-100, 1, 13)
    source = TelethonHistorySource(Client())
    received = [message.id async for message in source.iter_gap(gap)]

    assert received == [11, 12]
    assert calls == [(-100, {
        "limit": 1,
        "offset_date": msglog_backfill.RECOVERY_SCAN_START,
    }), (-100, {
        "min_id": 10,
        "max_id": 13,
        "reverse": True,
    })]


@pytest.mark.asyncio
async def test_telethon_history_uses_gap_bound_when_nothing_predates_cutoff():
    calls = []

    class Client:
        def iter_messages(self, chat_id, **kwargs):
            calls.append((chat_id, kwargs))

            async def messages():
                if "offset_date" not in kwargs:
                    for message_id in (1, 2, 3):
                        yield SimpleNamespace(id=message_id)

            return messages()

    gap = MsgLogGap(-100, 0, 4)
    source = TelethonHistorySource(Client())
    received = [message.id async for message in source.iter_gap(gap)]

    assert received == [1, 2, 3]
    assert calls == [(-100, {
        "limit": 1,
        "offset_date": msglog_backfill.RECOVERY_SCAN_START,
    }), (-100, {
        "min_id": 0,
        "max_id": 4,
        "reverse": True,
    })]


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
        await MsgLogGapBackfiller(
            MsgLogBackfillStore(), history, main_bot_id=1000, auxiliary_bot_ids=(2000,)
        ).run()

    assert history.started == [first, second]
    rows = list(MsgLog.select().where(
        MsgLog.master_msg_id.in_(["-100.2", "-100.3", "-100.4"])
    ).order_by(MsgLog.master_msg_id))
    assert [(row.master_msg_id, row.sender_bot_id, row.slave_origin_uid) for row in rows] == [
        ("-100.2", None, "tests.mocks.slave chat"),
        ("-100.3", "2000", "tests.mocks.slave chat"),
    ]


@pytest.mark.asyncio
async def test_backfill_excludes_messages_before_the_loss_cutoff(msglog_database):
    for message_id in (1, 23):
        _log(message_id)
    TopicAssoc.create(topic_chat_id="-100", message_thread_id="9", slave_uid="tests.mocks.slave chat")
    gap = MsgLogGap(-100, 1, 23)
    before_loss = _message(2, 1000)
    eastern = datetime.timezone(datetime.timedelta(hours=2))
    before_loss.date = datetime.datetime(2026, 7, 14, 20, 22, 2, tzinfo=eastern)
    at_loss = _message(3, 1000)
    at_loss.date = datetime.datetime(2026, 7, 14, 20, 22, 3, tzinfo=eastern)

    results = await MsgLogGapBackfiller(
        MsgLogBackfillStore(), _History({gap: [before_loss, at_loss]}), main_bot_id=1000
    ).run()

    assert results[0].skipped == {"before_loss": 1, "deleted": 19}
    assert MsgLog.get_or_none(MsgLog.master_msg_id == "-100.2") is None
    assert MsgLog.get_by_id("-100.3").time == at_loss.date


@pytest.mark.asyncio
async def test_backfill_merges_available_histories_in_message_order(msglog_database):
    for message_id in (1, 23):
        _log(message_id)
    TopicAssoc.create(topic_chat_id="-100", message_thread_id="9", slave_uid="tests.mocks.slave chat")
    gap = MsgLogGap(-100, 1, 23)
    primary = _History({gap: [_message(2, 1000), _message(4, 1000)]})
    auxiliary = _History({gap: [
        _message(2, 1000), _message(3, 2000), _message(5, 3000)
    ]})

    results = await MsgLogGapBackfiller(
        MsgLogBackfillStore(), [primary, auxiliary], main_bot_id=1000, auxiliary_bot_ids=(2000,)
    ).run()

    assert results[0].inserted == 3
    rows = list(MsgLog.select().where(
        MsgLog.master_msg_id.in_(["-100.2", "-100.3", "-100.4"])
    ).order_by(MsgLog.master_msg_id))
    assert [(row.master_msg_id, row.sender_bot_id) for row in rows] == [
        ("-100.2", None),
        ("-100.3", "2000"),
        ("-100.4", None),
    ]
    assert results[0].skipped == {"unconfigured_bot": 1, "deleted": 17}


@pytest.mark.asyncio
async def test_backfill_aborts_gap_without_writing_when_any_source_fails(msglog_database):
    for message_id in (1, 23, 50):
        _log(message_id)
    TopicAssoc.create(topic_chat_id="-100", message_thread_id="9", slave_uid="tests.mocks.slave chat")
    first_gap = MsgLogGap(-100, 1, 23)
    second_gap = MsgLogGap(-100, 23, 50)
    primary = _History({first_gap: [_message(2, 1000)]})
    failing = _History({}, fail_at=first_gap)
    auxiliary = _History({first_gap: [_message(3, 2000)]})

    with pytest.raises(RuntimeError, match="history failed"):
        await MsgLogGapBackfiller(
            MsgLogBackfillStore(), [primary, failing, auxiliary], main_bot_id=1000,
            auxiliary_bot_ids=(2000,)
        ).run()

    assert primary.started == [first_gap]
    assert failing.started == [first_gap]
    assert auxiliary.started == []
    assert MsgLog.select().where(MsgLog.master_msg_id.in_(["-100.2", "-100.3"])).count() == 0
    assert MsgLog.get_by_id("-100.50").text == "existing"
    assert MsgLogBackfillCheckpoint.get(MsgLogBackfillCheckpoint.left == 1).cursor == 2
    assert second_gap not in primary.started


@pytest.mark.asyncio
async def test_backfill_chunk_boundaries_resume_without_holes(msglog_database):
    class ChunkStore(MsgLogBackfillStore):
        CHUNK_MESSAGE_SPAN = 1000

    class AllMessagesHistory:
        def __init__(self, fail_at=None):
            self.fail_at = fail_at
            self.started = []

        async def iter_gap(self, gap):
            self.started.append(gap)
            if gap == self.fail_at:
                raise RuntimeError("history failed")
            for message_id in range(gap.left + 1, gap.right):
                yield _message(message_id, 1000)

    _log(2003)
    TopicAssoc.create(topic_chat_id="-100", message_thread_id="9", slave_uid="tests.mocks.slave chat")
    gap = MsgLogGap(-100, 0, 2003)
    failed_chunk = MsgLogGap(-100, 1000, 2001)
    store = ChunkStore()

    with pytest.raises(RuntimeError, match="history failed"):
        await MsgLogGapBackfiller(
            store, AllMessagesHistory(fail_at=failed_chunk), main_bot_id=1000
        ).run()

    assert MsgLogBackfillCheckpoint.get().cursor == 1001
    resumed_history = AllMessagesHistory()
    results = await MsgLogGapBackfiller(store, resumed_history, main_bot_id=1000).run()

    assert resumed_history.started == [failed_chunk, MsgLogGap(-100, 2000, 2003)]
    assert results == [GapResult(gap, 1002, {"deleted": 0})]
    assert MsgLogBackfillCheckpoint.select().count() == 0
    assert {
        int(row.master_msg_id.rsplit(".", 1)[1])
        for row in MsgLog.select(MsgLog.master_msg_id)
    } == set(range(1, 2004))


@pytest.mark.asyncio
async def test_command_starts_main_and_auxiliary_bot_history_sources(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'token: "1000:main"\n'
        'auxiliary_bots:\n'
        '  - token: "2000:auxiliary-one"\n'
        '  - token: "3000:auxiliary-two"\n'
    )
    clients = []
    captured = {}

    class Client:
        def __init__(self, session, api_id, api_hash, *, receive_updates):
            self.session = session
            self.api_id = api_id
            self.api_hash = api_hash
            self.receive_updates = receive_updates
            self.started_with = None
            self.verified = False
            self.disconnected = False
            clients.append(self)

        async def start(self, *, bot_token):
            self.started_with = bot_token

        async def get_me(self):
            self.verified = True
            return SimpleNamespace(id=int(self.started_with.partition(":")[0]), bot=True)

        async def disconnect(self):
            self.disconnected = True

    class Manager:
        stopped = False

        def __init__(self, channel):
            self.channel = channel

        def stop_worker(self):
            self.stopped = True

    class Backfiller:
        def __init__(self, store, histories, *, main_bot_id, auxiliary_bot_ids):
            captured["histories"] = histories
            captured["main_bot_id"] = main_bot_id
            captured["auxiliary_bot_ids"] = tuple(auxiliary_bot_ids)

        async def run(self):
            return []

    monkeypatch.setattr(msglog_backfill, "get_config_path", lambda channel_id: config_path)
    monkeypatch.setattr(msglog_backfill, "DatabaseManager", Manager)
    monkeypatch.setattr(msglog_backfill, "MsgLogGapBackfiller", Backfiller)
    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(TelegramClient=Client))

    results = await msglog_backfill._run_command(SimpleNamespace(
        profile="default", session=tmp_path / "backfill", api_id=1, api_hash="hash"
    ))

    assert results == []
    assert captured["main_bot_id"] == 1000
    assert captured["auxiliary_bot_ids"] == (2000, 3000)
    assert [client.started_with for client in clients] == [
        "1000:main", "2000:auxiliary-one", "3000:auxiliary-two"
    ]
    assert [client.session for client in clients] == [
        str(tmp_path / "backfill-1000"),
        str(tmp_path / "backfill-2000"),
        str(tmp_path / "backfill-3000"),
    ]
    assert all(client.receive_updates is False and client.verified and client.disconnected for client in clients)
    assert len(captured["histories"]) == 3


@pytest.mark.asyncio
async def test_command_aborts_when_a_configured_bot_session_is_not_verified(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'token: "1000:main"\n'
        'auxiliary_bots:\n'
        '  - token: "2000:auxiliary"\n'
    )
    clients = []

    class Client:
        def __init__(self, session, api_id, api_hash, *, receive_updates):
            self.started_with = None
            self.disconnected = False
            clients.append(self)

        async def start(self, *, bot_token):
            self.started_with = bot_token

        async def get_me(self):
            if self.started_with.startswith("1000:"):
                return SimpleNamespace(id=1000, bot=True)
            return SimpleNamespace(id=9999, bot=False)

        async def disconnect(self):
            self.disconnected = True

    class Manager:
        def __init__(self, channel):
            self.channel = channel

        def stop_worker(self):
            pass

    monkeypatch.setattr(msglog_backfill, "get_config_path", lambda channel_id: config_path)
    monkeypatch.setattr(msglog_backfill, "DatabaseManager", Manager)
    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(TelegramClient=Client))

    with pytest.raises(RuntimeError, match="configured Telegram bot 2000"):
        await msglog_backfill._run_command(SimpleNamespace(
            profile="default", session=tmp_path / "backfill", api_id=1, api_hash="hash"
        ))

    assert len(clients) == 2
    assert all(client.disconnected for client in clients)


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


def test_chunk_write_rolls_back_cursor_and_preserves_existing_rows(msglog_database):
    class FailingStore(MsgLogBackfillStore):
        INSERT_CHUNK_SIZE = 1

        def __init__(self):
            self.calls = 0

        def _insert_batch(self, rows):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("write failed")
            return super()._insert_batch(rows)

    _log(1)
    _log(23)
    store = FailingStore()
    pending = store.snapshot_gaps()[0]
    with pytest.raises(RuntimeError, match="write failed"):
        store.insert_chunk_and_advance(pending, 23, [_row(2), _row(3)])
    assert MsgLog.select().where(MsgLog.master_msg_id.in_(["-100.2", "-100.3"])).count() == 0
    assert MsgLogBackfillCheckpoint.get().cursor == 2

    _log(2)
    store = MsgLogBackfillStore()
    assert store.insert_chunk_and_advance(store.snapshot_gaps()[0], 23, [_row(2, "replacement"), _row(3)]) == 1
    assert MsgLog.get_by_id("-100.2").text == "existing"
    assert MsgLog.get_by_id("-100.3").text == "new"


def test_alternate_id_is_not_a_gap_anchor_or_overwritten(msglog_database):
    _log(1)
    _log(50)
    MsgLog.update(master_msg_id_alt="-100.23").where(MsgLog.master_msg_id == "-100.50").execute()

    assert MsgLogBackfillStore().find_gaps() == [MsgLogGap(-100, 1, 50)]
    store = MsgLogBackfillStore()
    pending = store.snapshot_gaps()[0]
    assert store.insert_chunk_and_advance(pending, 50, [_row(23), _row(24)]) == 1
    assert MsgLog.get_by_id("-100.50").master_msg_id_alt == "-100.23"
    assert MsgLog.get_or_none(MsgLog.master_msg_id == "-100.23") is None
    assert MsgLog.get_by_id("-100.24").text == "new"


def test_snapshot_keeps_a_gap_after_live_rows_fragment_it(msglog_database):
    _log(1)
    _log(43)
    store = MsgLogBackfillStore()

    assert store.snapshot_gaps() == [PendingGap(MsgLogGap(-100, 1, 43), 2)]
    for message_id in range(2, 23):
        _log(message_id)

    assert store.find_gaps() == []
    assert store.snapshot_gaps() == [PendingGap(MsgLogGap(-100, 1, 43), 2)]


def test_snapshot_advances_interior_legacy_checkpoint_to_the_first_unread_message(msglog_database):
    _log(1)
    MsgLogBackfillCheckpoint.create(chat_id=-100, left=1, right=43, cursor=1)

    assert MsgLogBackfillStore().snapshot_gaps() == [PendingGap(MsgLogGap(-100, 1, 43), 2)]


@pytest.mark.asyncio
async def test_legacy_leading_checkpoint_includes_message_id_one(msglog_database):
    MsgLogBackfillCheckpoint.create(chat_id=-100, left=1, right=43, cursor=1)
    TopicAssoc.create(topic_chat_id="-100", message_thread_id="9", slave_uid="tests.mocks.slave chat")
    legacy_gap = MsgLogGap(-100, 1, 43)
    normalized_gap = MsgLogGap(-100, 0, 43)
    history = _History({normalized_gap: [_message(1, 1000)]})
    store = MsgLogBackfillStore()

    assert store.snapshot_gaps() == [PendingGap(normalized_gap, 1)]
    checkpoint = MsgLogBackfillCheckpoint.get()
    assert (checkpoint.left, checkpoint.cursor) == (0, 1)

    await MsgLogGapBackfiller(store, history, main_bot_id=1000).run()

    assert history.started == [normalized_gap]
    assert MsgLog.get_by_id("-100.1").text == "content"
    assert MsgLogBackfillCheckpoint.select().count() == 0


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

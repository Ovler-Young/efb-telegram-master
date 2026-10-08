import copy
import datetime
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ehforwarderbot import MsgType
from ehforwarderbot.chat import ChatMember, GroupChat
from ehforwarderbot.message import MessageCommand, MessageCommands, Substitutions

from efb_telegram_master.aggregate import (
    aggregate_fits, make_aggregate, make_source_member, member_message, render_members, utf16_length,
)
from efb_telegram_master import migrate_db
from efb_telegram_master.db import DatabaseManager, MsgLog, MsgLogMember, database
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.msg_type import TGMsgType


@pytest.fixture
def manager(tmp_path, monkeypatch):
    original_database = database.obj
    monkeypatch.setattr("efb_telegram_master.db.utils.get_data_path", lambda _: tmp_path)
    manager = DatabaseManager(SimpleNamespace(channel_id="tests.aggregate", config={}))
    try:
        yield manager
    finally:
        manager.stop_worker()
        database.initialize(original_database)


def source(uid="one", text="body", name="Alice"):
    chat = GroupChat(module_id="tests.source", uid="group", name="Group", with_self=True)
    author = ChatMember(chat, uid=name.lower(), name=name)
    return ETMMsg(uid=uid, chat=chat, author=author, text=text,
                  type=MsgType.Text, type_telegram=TGMsgType.Text,
                  deliver_to=SimpleNamespace(channel_id="tests.master"))


def receipt(number=1, chat_id=-100, topic=None):
    return SimpleNamespace(chat_id=chat_id, message_id=number, message_thread_id=topic)


class SourceCache:
    def get_chat(self, module, uid, **_):
        return GroupChat(module_id=module, uid=uid, with_self=False)

    def get_chat_member(self, module, group, uid, **_):
        return ChatMember(self.get_chat(module, group), uid=uid)


def test_member_roundtrip_mapping_and_legacy_restoration(manager):
    messages = [source("one", "same"), source("two", "same", "Bob")]
    members = [make_source_member(msg) for msg in messages]
    aggregate = make_aggregate(members, 1)
    row = manager.finalize_aggregate_message(receipt(topic=3), aggregate, "700")

    assert len(manager.get_container_members(row.master_msg_id)) == 2
    assert MsgLogMember.select().count() == 2

    with sqlite3.connect(str(manager._base_path / "tgdata.db")) as exported:
        migrate_db._validate_source(exported)
        assert MsgLogMember in migrate_db.MODELS
        assert len(list(migrate_db._source_rows(exported, MsgLogMember))) == 2
    assert not any(index.unique for index in database.get_indexes("msglogmember")
                   if index.columns == ["slave_origin_uid", "slave_message_id"])
    resolved_row, member = manager.resolve_source_member(members[1]["origin_uid"], "two", "-100", "3")
    restored = resolved_row.build_source_member(member, SourceCache())
    assert (restored.uid, restored.text, restored.author.uid, restored.sender_bot_id) == ("two", "same", "bob", "700")
    assert manager.resolve_source_member(members[1]["origin_uid"], "two", "-200", "3") is None
    with pytest.raises(ValueError, match="Select a source member"):
        row.build_etm_msg(SourceCache())

    legacy = source("legacy", "original")
    manager.add_or_update_message_log(legacy, receipt(2))
    legacy_row, legacy_member = manager.resolve_source_member(members[0]["origin_uid"], "legacy", "-100")
    assert legacy_member is None
    assert legacy_row.build_etm_msg(SourceCache()).text == "original"

    reply = source("reply", "reply body")
    reply.target = messages[1]
    manager.add_or_update_message_log(reply, receipt(3, topic=3))
    restored_reply = manager.get_msg_log(master_msg_id="-100.3").build_etm_msg(SourceCache())
    assert (restored_reply.target.uid, restored_reply.target.author.uid) == ("two", "bob")


def test_finalization_is_idempotent_and_preserves_newer_removed_source(manager):
    member = make_source_member(source())
    manager.finalize_aggregate_message(receipt(), make_aggregate([member], 1))
    removed = copy.deepcopy(member)
    removed.update(status="removed", source_revision=2)
    manager.finalize_aggregate_message(receipt(), make_aggregate([removed], 2))
    manager.finalize_aggregate_message(receipt(), make_aggregate([member], 1))
    manager.finalize_aggregate_message(receipt(), make_aggregate([removed], 2))
    assert MsgLogMember.select().count() == 1
    assert MsgLog.get().aggregate["revision"] == 2
    assert member_message(MsgLog.get().aggregate["children"][0]).text == "body"

    # An in-flight older source snapshot can confirm its real presentation;
    # it does not reverse a newer logical source status.
    manager.finalize_aggregate_message(receipt(), make_aggregate([member], 3))
    row = MsgLog.get()
    assert row.text == render_members([member]).text
    assert row.aggregate["children"][0]["status"] == "removed"


def test_redirect_is_atomic_and_preserves_confirmed_presentation_and_commands(manager):
    member = make_source_member(source())
    aggregate = make_aggregate([member], 1)
    manager.finalize_aggregate_message(receipt(), aggregate, "700")
    edited = source(text="new source text")
    edited.commands = MessageCommands([MessageCommand(name="Action", callable_name="do_action")])
    replacement_member = make_source_member(edited, source_revision=2)

    original_save = MsgLog.save

    def fail_old_route(row, *args, **kwargs):
        if row.master_msg_id == "-100.1" and row.aggregate["children"][0]["status"] == "redirected":
            raise RuntimeError("write failed")
        return original_save(row, *args, **kwargs)

    with patch.object(MsgLog, "save", fail_old_route):
        with pytest.raises(RuntimeError, match="write failed"):
            manager.finalize_member_redirect("-100.1", replacement_member, edited, receipt(2), "800")
    assert MsgLog.select().count() == 1
    assert MsgLogMember.select().count() == 1
    assert MsgLog.get().aggregate["children"][0]["status"] == "active"

    output = manager.finalize_member_redirect("-100.1", replacement_member, edited, receipt(2), "800")
    manager.finalize_member_redirect("-100.1", replacement_member, edited, receipt(2), "800")
    old = manager.get_msg_log(master_msg_id="-100.1")
    assert (old.text, old.aggregate["confirmed_ranges"]) == (aggregate["confirmed_text"], aggregate["confirmed_ranges"])
    assert old.aggregate["children"][0]["replacement_master_msg_id"] == "-100.2"
    assert MsgLogMember.select().count() == 2
    assert output.build_etm_msg(SourceCache()).commands[0].name == "Action"
    resolved, selected = manager.resolve_source_member(member["origin_uid"], "one", "-100")
    assert (resolved.master_msg_id, selected["source_revision"]) == ("-100.2", 2)
    assert render_members(old.aggregate["children"], history=True).ranges == []
    assert render_members([output.source_member], history=True).text.endswith("new source text")

    newer = source(text="latest standalone source")
    newer_member = make_source_member(newer, source_revision=3)
    manager.finalize_source_message(newer, receipt(3), newer_member, "800", old_message_id=(-100, 2))
    manager.finalize_source_message(edited, receipt(4), replacement_member, "800", old_message_id=(-100, 3))
    independent = manager.get_msg_log(master_msg_id="-100.2")
    assert (independent.text, independent.master_msg_id_alt, independent.source_member["source_revision"]) == (
        "latest standalone source", "-100.3", 3)
    assert MsgLogMember.select().count() == 2

    latest = source(text="updated through alternate receipt")
    latest_member = make_source_member(latest, source_revision=4)
    manager.finalize_source_message(latest, receipt(3), latest_member, "800")
    assert MsgLog.select().count() == 2
    assert manager.get_msg_log(master_msg_id="-100.2").source_member["source_revision"] == 4

    active_snapshot = copy.deepcopy(latest_member)
    manager.finalize_aggregate_message(receipt(), make_aggregate([active_snapshot], 2), "700")
    old = manager.get_msg_log(master_msg_id="-100.1")
    assert old.aggregate["children"][0]["status"] == "redirected"
    assert old.aggregate["children"][0]["replacement_master_msg_id"] == "-100.2"
    assert manager.resolve_source_member(member["origin_uid"], "one", "-100")[0].master_msg_id == "-100.2"

def test_rendering_preserves_identities_reply_and_utf16_ranges():
    first = source("one", "😀 <same>", "A & B")
    first.substitutions = Substitutions({(2, 8): first.chat.self})
    second = source("two", first.text, "Bob")
    second.target = first
    source_time = datetime.datetime(2026, 10, 8, 4, 34, 56, tzinfo=datetime.timezone.utc)
    received_time = source_time + datetime.timedelta(seconds=2)
    members = [make_source_member(first, source_time=source_time, received_time=received_time),
               make_source_member(second, received_time=received_time, display_prefix="Bob:")]
    rendered = render_members(members, admin_id=123)
    assert '<b>A &amp; B:</b> <code>12:34:56</code>\n' in rendered.html
    assert '<a href="tg://user?id=123">&lt;same&gt;</a>' in rendered.html
    assert rendered.text.count("😀 <same>") == 3
    assert "tests.source group/one" in rendered.text
    assert [item["source_id"] for item in rendered.ranges] == ["one", "two"]
    assert rendered.ranges[0]["end"] == utf16_length("A & B: 12:34:56\n😀 <same>")
    assert rendered.ranges[1]["start"] == rendered.ranges[0]["end"] + 2
    assert "Bob: 12:34:58\n" in rendered.text
    assert "<b>Bob:</b> <code>12:34:58</code>\n" in rendered.html
    assert member_message(members[1]).target.uid == "one"


def test_capacity_uses_parsed_text_and_keeps_full_source_with_bounded_tombstones():
    member = make_source_member(source(text="&" * 4000, name="A"))
    assert len(render_members([member]).html) > 4096
    assert aggregate_fits([member])
    assert not aggregate_fits([member], max_payload_bytes=10)
    assert not aggregate_fits([member], max_payload_bytes=10, include_redirected_payload=False)
    assert not aggregate_fits([member], max_members=0)
    long_member = make_source_member(source(text="body" * 1100))
    assert not aggregate_fits([long_member])
    assert not aggregate_fits([long_member], include_redirected_payload=False)
    assert member_message(long_member).text == "body" * 1100

    redirected = make_source_member(source(text="x" * 270000))
    redirected["status"] = "redirected"
    assert not aggregate_fits([redirected])
    assert aggregate_fits([redirected], include_redirected_payload=False)
    assert not aggregate_fits([redirected], max_members=0, include_redirected_payload=False)

    received_time = datetime.datetime(2026, 10, 8, 12, 34, 56, tzinfo=datetime.timezone.utc)
    removed = [make_source_member(source(str(index), "saved", "name" * 40), received_time=received_time)
               for index in range(200)]
    for member in removed:
        member["status"] = "removed"
    for member in removed[100:]:
        member["received_time"] += datetime.timedelta(days=1)
    rendered = render_members(removed)
    assert 0 < len(rendered.text) <= 4096
    assert len(rendered.ranges) == 200
    assert rendered.text.count("08:34:56") == 200
    assert aggregate_fits(removed)
    assert render_members(removed, history=True).text.count("saved") == 200

"""Aggregation recovery must not read queued payloads."""
import pickle

from efb_telegram_master.outbound import OutboundQueue, QueueRequest
from tests.unit.test_outbound import enqueue


def test_aggregation_rows_return_identity_without_payloads(tmp_path):
    """Recovery reads contexts and ordering only, never the stored media."""
    from efb_telegram_master.queued_log import decode_aggregation, encode_aggregation

    queue = OutboundQueue(tmp_path)
    context = encode_aggregation({
        "format_version": 1, "kind": "aggregate", "key": ["slave.origin", -100, None],
        "members": [], "children": [], "generation": 0, "recent": [], "last_new": 0.0,
    })
    row_id, _waiter = enqueue(queue, QueueRequest(
        "send_message", (), {"chat_id": 7, "text": "aggregated"}, context,
    ))
    plain_id, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "text"}))
    try:
        rows = queue.aggregation_rows()
        assert [row.id for row in rows] == [row_id]
        assert decode_aggregation(rows[0].log_context)["key"] == ["slave.origin", -100, None]
        assert rows[0].payload == b"", "aggregation recovery materialised a stored payload"
        # The payload is still intact and reachable when the row is dispatched.
        assert queue.load_queued(row_id).payload
        assert plain_id not in [row.id for row in rows]
    finally:
        queue.close()


def test_aggregation_rows_skip_legacy_inline_media(tmp_path):
    """A legacy row with the whole file inline must not be read by recovery."""
    from efb_telegram_master.queued_log import decode_aggregation, encode_aggregation

    queue = OutboundQueue(tmp_path)
    context = encode_aggregation({
        "format_version": 1, "kind": "source", "key": ["slave.origin", -100, None],
        "members": [], "generation": 0, "recent": [], "last_new": 0.0,
    })
    legacy = b"\x01" + pickle.dumps({"chat_id": 7, "document": b"x" * 4096})
    with queue.connection:
        queue.connection.execute(
            "INSERT INTO outbound_queue(priority,telegram_chat_id,operation,payload,created_at,log_context) "
            "VALUES(0,7,'send_document',?,1000.0,?)", (legacy, context),
        )
    try:
        rows = queue.aggregation_rows()
        assert len(rows) == 1
        assert rows[0].payload == b""
        assert decode_aggregation(rows[0].log_context)["kind"] == "source"
    finally:
        queue.close()

"""The startup sidecar sweep must not make startup scale with the backlog."""
import logging
import pickle


def _queue_with_rows(tmp_path, rows=3):
    from efb_telegram_master.outbound import OutboundQueue

    queue = OutboundQueue(tmp_path)
    with queue.connection:
        queue.connection.executemany(
            "INSERT INTO outbound_queue(priority,telegram_chat_id,operation,payload,created_at) "
            "VALUES(0,7,'send_message',?,1000.0)",
            [(b"\x02" + pickle.dumps({}),) for _ in range(rows)],
        )
    return queue


def test_large_queue_skips_the_sweep_and_keeps_sidecars(tmp_path, monkeypatch, caplog):
    from efb_telegram_master.outbound import OutboundQueue

    queue = _queue_with_rows(tmp_path)
    orphan = queue.media_dir / "orphan.bin"
    orphan.write_bytes(b"keep me")
    queue.close()

    monkeypatch.setattr(OutboundQueue, "MAX_ORPHAN_SCAN_ROWS", 2)
    with caplog.at_level(logging.WARNING, logger="efb_telegram_master.outbound"):
        restarted = OutboundQueue(tmp_path)
    try:
        assert orphan.exists()
        assert "Skipping the outbound media sweep" in caplog.records[-1].message
    finally:
        restarted.close()


def test_small_queue_still_sweeps_orphans(tmp_path, monkeypatch):
    from efb_telegram_master.outbound import OutboundQueue

    queue = _queue_with_rows(tmp_path)
    orphan = queue.media_dir / "orphan.bin"
    orphan.write_bytes(b"sweep me")
    queue.close()

    monkeypatch.setattr(OutboundQueue, "MAX_ORPHAN_SCAN_ROWS", 3)
    restarted = OutboundQueue(tmp_path)
    try:
        assert not orphan.exists()
    finally:
        restarted.close()

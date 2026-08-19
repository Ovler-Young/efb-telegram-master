import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ehforwarderbot.constants import MsgType

from efb_telegram_master.slave_delivery_types import DeliveryPlan
from efb_telegram_master.slave_message import SlaveMessageService
from efb_telegram_master.slave_message_claims import SlaveMessageClaimLifecycle


def _message(uid="message"):
    return SimpleNamespace(
        uid=uid,
        edit=False,
        type=MsgType.Text,
        chat=SimpleNamespace(module_id="tests.slave", uid="chat"),
    )


def _dedupe_processor() -> SlaveMessageService:
    processor = object.__new__(SlaveMessageService)
    processor.msglogs = Mock()
    processor.logger = Mock()
    processor.router = Mock(route=Mock(return_value=DeliveryPlan("template", 123, None)))
    processor.is_silent = Mock(return_value=False)
    processor.dispatch_message = Mock()
    delivery_claims = Mock()
    delivery_claims.claim.return_value = "claim-token"
    processor.claim_lifecycle = SlaveMessageClaimLifecycle(delivery_claims, processor.logger)
    return processor


@pytest.mark.parametrize(
    "message",
    [
        SimpleNamespace(edit=True, uid="message", type=MsgType.Text),
        SimpleNamespace(edit=False, uid=None, type=MsgType.Text),
        SimpleNamespace(edit=False, uid="message", type=MsgType.Status),
    ],
)
def test_dedupe_key_excludes_nondeliverable_message_forms(message) -> None:
    assert SlaveMessageClaimLifecycle.dedupe_key(message, "tests.slave chat") is None


def test_new_slave_message_claims_durable_dedupe_without_msglog_lookup() -> None:
    processor = _dedupe_processor()
    message = _message()

    assert processor.send_message(message) is message
    processor.claim_lifecycle.delivery_claims.claim.assert_called_once_with("tests.slave chat", "message")
    processor.msglogs.get_msg_log.assert_not_called()
    processor.dispatch_message.assert_called_once()
    assert processor.dispatch_message.call_args.args == (message, "template", None, 123, None, False)
    assert processor.dispatch_message.call_args.kwargs["dedupe_key"] == ("tests.slave chat", "message")
    assert processor.dispatch_message.call_args.kwargs["claim_token"] == "claim-token"
    assert not processor.dispatch_message.call_args.kwargs["ownership_lost"].is_set()


def test_pending_duplicate_and_muted_message_do_not_dispatch() -> None:
    processor = _dedupe_processor()
    processor.claim_lifecycle.delivery_claims.claim.return_value = None
    assert processor.send_message(_message()) is not None
    processor.dispatch_message.assert_not_called()

    processor = _dedupe_processor()
    processor.is_silent.return_value = None
    assert processor.send_message(_message()) is not None
    processor.claim_lifecycle.delivery_claims.release.assert_called_once_with("tests.slave chat", "message", "claim-token")
    processor.dispatch_message.assert_not_called()


def test_destination_mapping_failure_releases_the_pending_dedupe_claim() -> None:
    processor = _dedupe_processor()
    processor.router.route.side_effect = RuntimeError("database unavailable")

    assert processor.send_message(_message()) is not None
    processor.claim_lifecycle.delivery_claims.release.assert_called_once_with("tests.slave chat", "message", "claim-token")
    processor.dispatch_message.assert_not_called()


def test_terminal_delivery_failure_releases_the_dedupe_claim_without_completing_it() -> None:
    processor = _dedupe_processor()
    processor.dispatch_message.side_effect = ValueError("attachment failed")

    assert processor.send_message(_message()) is not None
    processor.claim_lifecycle.delivery_claims.release.assert_called_once_with("tests.slave chat", "message", "claim-token")
    processor.claim_lifecycle.delivery_claims.complete.assert_not_called()


def test_active_delivery_renews_the_owned_claim() -> None:
    processor = _dedupe_processor()
    processor.claim_lifecycle.RENEW_INTERVAL = 0.01
    started, release = threading.Event(), threading.Event()

    def dispatch(*_args, **_kwargs):
        started.set()
        assert release.wait(1)

    processor.dispatch_message.side_effect = dispatch
    worker = threading.Thread(target=processor.send_message, args=(_message(),))
    worker.start()
    try:
        assert started.wait(1)
        deadline = time.monotonic() + 1
        while not processor.claim_lifecycle.delivery_claims.renew.called and time.monotonic() < deadline:
            time.sleep(0.01)
        processor.claim_lifecycle.delivery_claims.renew.assert_called_with("tests.slave chat", "message", "claim-token")
    finally:
        release.set()
        worker.join(1)


def test_renewal_exception_fences_post_send_side_effects() -> None:
    processor = _dedupe_processor()
    processor.claim_lifecycle.RENEW_INTERVAL = 0.01
    processor.claim_lifecycle.delivery_claims.renew.side_effect = RuntimeError("database unavailable")
    started, release = threading.Event(), threading.Event()

    def dispatch(*_args, **_kwargs):
        started.set()
        assert release.wait(1)

    processor.dispatch_message.side_effect = dispatch
    worker = threading.Thread(target=processor.send_message, args=(_message(),))
    worker.start()
    try:
        assert started.wait(1)
        ownership_lost = processor.dispatch_message.call_args.kwargs["ownership_lost"]
        deadline = time.monotonic() + 1
        while not ownership_lost.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ownership_lost.is_set()
        processor.logger.exception.assert_called_once_with("Failed to renew delivery claim (%s).", "RuntimeError")
    finally:
        release.set()
        worker.join(1)


def test_ingested_message_edit_has_no_telegram_side_effect() -> None:
    processor = _dedupe_processor()
    processor.msglogs.get_msg_log.return_value = SimpleNamespace(provenance="mtproto_ingested")
    message = _message("mtproto-ingested:100.1")
    message.edit = True

    assert processor.send_message(message) is message
    processor.router.route.assert_not_called()
    processor.dispatch_message.assert_not_called()

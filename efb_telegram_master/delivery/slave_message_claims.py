"""Durable claim ownership for slave-message delivery."""

import logging
import threading
from contextlib import contextmanager
from typing import Optional, Tuple

from ehforwarderbot import Message
from ehforwarderbot.constants import MsgType


class SlaveMessageClaimLifecycle:
    """Own the claim, renewal, completion, and release lifecycle for one delivery."""

    RENEW_INTERVAL = 60.0

    def __init__(self, delivery_claims, logger: logging.Logger) -> None:
        self.delivery_claims = delivery_claims
        self.logger = logger

    @staticmethod
    def dedupe_key(msg: Message, slave_origin_uid: str) -> Optional[Tuple[str, str]]:
        if msg.edit or msg.uid is None or msg.type == MsgType.Status:
            return None
        return slave_origin_uid, str(msg.uid)

    def claim(self, key: Tuple[str, str]) -> Optional[str]:
        return self.delivery_claims.claim(*key)

    def complete(self, key: Optional[Tuple[str, str]], owner_token: Optional[str]) -> bool:
        return key is not None and owner_token is not None and self.delivery_claims.complete(*key, owner_token)

    def release(self, key: Optional[Tuple[str, str]], owner_token: Optional[str]) -> None:
        if key is not None and owner_token is not None:
            self.delivery_claims.release(*key, owner_token)

    @contextmanager
    def renew(self, key: Optional[Tuple[str, str]], owner_token: Optional[str]):
        if key is None or owner_token is None:
            yield None
            return
        stopped, ownership_lost = threading.Event(), threading.Event()

        def renew_claim() -> None:
            while not stopped.wait(self.RENEW_INTERVAL):
                try:
                    renewed = self.delivery_claims.renew(*key, owner_token)
                except Exception as error:
                    self.logger.exception("Failed to renew delivery claim (%s).", type(error).__name__)
                    ownership_lost.set()
                    return
                if not renewed:
                    ownership_lost.set()
                    return

        worker = threading.Thread(target=renew_claim, daemon=True, name="SlaveMessageClaimRenewal")
        worker.start()
        try:
            yield ownership_lost
        finally:
            stopped.set()
            worker.join(timeout=1)

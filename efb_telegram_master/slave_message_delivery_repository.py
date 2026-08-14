import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from peewee import IntegrityError

from .database_observability import ObservedRepository, observe_database_method
from .models import SlaveMessageDelivery
from .utils import EFBChannelChatIDStr


class SlaveMessageDeliveryRepository(ObservedRepository):
    logger = logging.getLogger(__name__)
    LEASE_SECONDS = 300

    @observe_database_method("claim_slave_message_delivery")
    def claim(self, slave_origin_uid: EFBChannelChatIDStr, slave_message_id: str, lease_seconds: int = LEASE_SECONDS) -> Optional[str]:
        now = datetime.now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        owner_token = str(uuid4())
        try:
            SlaveMessageDelivery.create(
                slave_origin_uid=slave_origin_uid,
                slave_message_id=slave_message_id,
                state="pending",
                lease_expires_at=lease_expires_at,
                owner_token=owner_token,
            )
        except IntegrityError:
            reclaimed = (
                SlaveMessageDelivery.update(state="pending", lease_expires_at=lease_expires_at, owner_token=owner_token)
                .where(
                    (SlaveMessageDelivery.slave_origin_uid == slave_origin_uid)
                    & (SlaveMessageDelivery.slave_message_id == slave_message_id)
                    & (SlaveMessageDelivery.state == "pending")
                    & (SlaveMessageDelivery.lease_expires_at.is_null(True) | (SlaveMessageDelivery.lease_expires_at <= now))
                )
                .execute()
                == 1
            )
            return owner_token if reclaimed else None
        return owner_token

    @observe_database_method("complete_slave_message_delivery")
    def complete(self, slave_origin_uid: EFBChannelChatIDStr, slave_message_id: str, owner_token: str) -> bool:
        return (
            SlaveMessageDelivery.update(state="delivered", lease_expires_at=None)
            .where(
                (SlaveMessageDelivery.slave_origin_uid == slave_origin_uid)
                & (SlaveMessageDelivery.slave_message_id == slave_message_id)
                & (SlaveMessageDelivery.state == "pending")
                & (SlaveMessageDelivery.owner_token == owner_token)
            )
            .execute()
            == 1
        )

    @observe_database_method("renew_slave_message_delivery")
    def renew(self, slave_origin_uid: EFBChannelChatIDStr, slave_message_id: str, owner_token: str, lease_seconds: int = LEASE_SECONDS) -> bool:
        lease_expires_at = datetime.now() + timedelta(seconds=lease_seconds)
        return (
            SlaveMessageDelivery.update(lease_expires_at=lease_expires_at)
            .where(
                (SlaveMessageDelivery.slave_origin_uid == slave_origin_uid)
                & (SlaveMessageDelivery.slave_message_id == slave_message_id)
                & (SlaveMessageDelivery.state == "pending")
                & (SlaveMessageDelivery.owner_token == owner_token)
            )
            .execute()
            == 1
        )

    @observe_database_method("release_slave_message_delivery")
    def release(self, slave_origin_uid: EFBChannelChatIDStr, slave_message_id: str, owner_token: str) -> bool:
        return (
            SlaveMessageDelivery.delete()
            .where(
                (SlaveMessageDelivery.slave_origin_uid == slave_origin_uid)
                & (SlaveMessageDelivery.slave_message_id == slave_message_id)
                & (SlaveMessageDelivery.state == "pending")
                & (SlaveMessageDelivery.owner_token == owner_token)
            )
            .execute()
            == 1
        )

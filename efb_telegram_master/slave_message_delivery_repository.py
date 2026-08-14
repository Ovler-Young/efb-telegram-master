import logging

from peewee import IntegrityError

from .database_observability import ObservedRepository, observe_database_method
from .models import SlaveMessageDelivery
from .utils import EFBChannelChatIDStr


class SlaveMessageDeliveryRepository(ObservedRepository):
    logger = logging.getLogger(__name__)

    @observe_database_method("claim_slave_message_delivery")
    def claim(self, slave_origin_uid: EFBChannelChatIDStr, slave_message_id: str) -> bool:
        try:
            SlaveMessageDelivery.create(slave_origin_uid=slave_origin_uid, slave_message_id=slave_message_id)
        except IntegrityError:
            return False
        return True

    @observe_database_method("release_slave_message_delivery")
    def release(self, slave_origin_uid: EFBChannelChatIDStr, slave_message_id: str) -> None:
        SlaveMessageDelivery.delete().where((SlaveMessageDelivery.slave_origin_uid == slave_origin_uid) & (SlaveMessageDelivery.slave_message_id == slave_message_id)).execute()

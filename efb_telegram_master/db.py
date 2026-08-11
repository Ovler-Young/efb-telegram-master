# coding=utf-8

import datetime
import logging
import pickle
import time
from functools import partial, wraps
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Protocol, Tuple

from ehforwarderbot import Message as EFBMessage
from ehforwarderbot import utils
from ehforwarderbot.types import ChatID, MessageID, ModuleID
from peewee import DoesNotExist, IntegrityError, PostgresqlDatabase, SqliteDatabase, fn
from telegram import Message

from .message import ETMMsg
from .models import ChatAssoc, HistoryMigrationEntry, MsgLog, MsgLogIngestionLeaseLostError, MsgLogIngestionScan, PickledDict, SlaveChatInfo, TopicAssoc, database
from .utils import EFBChannelChatIDStr, OldMsgID, TelegramChatID, TelegramMessageID, TelegramTopicID, TgChatMsgIDStr, chat_id_str_to_id, chat_id_to_str, message_id_to_str

if TYPE_CHECKING:
    from . import TelegramChannel
    from .chat import ETMChatMixin


class DatabaseMetrics(Protocol):
    """Metrics interface injected by the bot manager after construction."""

    def record_database_method_call(self, method: str, seconds: float, outcome: str) -> None: ...


def observe_database_method(method: str):
    """Measure one public database operation with a statically bounded method label."""

    def decorate(call: Callable):
        @wraps(call)
        def wrapped(manager: "DatabaseManager", *args, **kwargs):
            started = time.perf_counter()
            outcome = "success"
            try:
                return call(manager, *args, **kwargs)
            except Exception:
                outcome = "failure"
                raise
            finally:
                metrics = getattr(manager, "_metrics", None)
                if metrics is not None:
                    try:
                        metrics.record_database_method_call(method, time.perf_counter() - started, outcome)
                    except Exception:
                        manager.logger.exception("Unable to record database method metric: %s", method)

        return wrapped

    return decorate


class DatabaseManager:
    logger = logging.getLogger(__name__)
    FAIL_FLAG = "__fail__"
    _LEGACY_OUTBOUND_TABLES = ("outbound_workflow", "outbound_task")
    _LEGACY_OUTBOUND_STATES = (
        "waiting_dependency",
        "queued",
        "leased",
        "in_flight",
        "sent_pending_log",
        "completed",
        "skipped",
        "dead",
    )

    def __init__(self, channel: "TelegramChannel"):
        self.channel: "TelegramChannel" = channel
        self._metrics: Optional[DatabaseMetrics] = None
        base_path = utils.get_data_path(channel.channel_id)
        self._base_path = base_path

        self.logger.debug("Loading database...")
        db_config = channel.config.get("database", {})
        db_type = db_config.get("type", "sqlite")

        if db_type == "postgresql":
            from playhouse.postgres_ext import PooledPostgresqlExtDatabase

            actual_db = PooledPostgresqlExtDatabase(
                db_config.get("database", "efb_telegram"),
                host=db_config.get("host", "localhost"),
                port=db_config.get("port", 5432),
                user=db_config.get("user", "postgres"),
                password=db_config.get("password", ""),
                max_connections=db_config.get("max_connections", 8),
                stale_timeout=db_config.get("stale_timeout", 300),
                options=db_config.get("options", "-c timezone=UTC"),
            )
        else:
            from peewee import SqliteDatabase

            actual_db = SqliteDatabase(
                str(base_path / "tgdata.db"),
                pragmas={
                    "journal_mode": "wal",
                    "foreign_keys": 1,
                    "busy_timeout": 5000,
                },
                check_same_thread=False,
            )

        database.initialize(actual_db)
        database.connect()
        self.logger.debug("Database loaded.")

        self.logger.debug("Checking database migration...")
        self._create()
        self.logger.debug("Database migration finished...")
        self._observe_legacy_outbound_rows()

    def set_metrics(self, metrics: DatabaseMetrics) -> None:
        """Attach the metrics recorder created after the database manager."""
        self._metrics = metrics

    @observe_database_method("stop_worker")
    def stop_worker(self):
        stop = getattr(database.obj, "stop", None)
        if callable(stop):
            stop()
        database.close()

    @staticmethod
    def _create():
        """
        Initializing tables.
        """
        database.create_tables(
            [
                ChatAssoc,
                MsgLog,
                SlaveChatInfo,
                TopicAssoc,
                HistoryMigrationEntry,
                MsgLogIngestionScan,
            ]
        )
        DatabaseManager._ensure_msglog_provenance()

    @staticmethod
    def _ensure_msglog_provenance() -> None:
        """Add the required MsgLog provenance column to databases created before it existed."""
        current_database = database.obj
        transaction_arguments: Tuple[str, ...] = ()
        if isinstance(current_database, SqliteDatabase):
            transaction_arguments = ("IMMEDIATE",)
        elif isinstance(current_database, PostgresqlDatabase):
            pass
        else:
            raise TypeError(f"Unsupported database backend: {type(current_database).__name__}")

        with current_database.atomic(*transaction_arguments):
            if isinstance(current_database, PostgresqlDatabase):
                current_database.execute_sql('LOCK TABLE "msglog" IN ACCESS EXCLUSIVE MODE')
            column_names = {column.name for column in current_database.get_columns(MsgLog._meta.table_name)}
            if "provenance" not in column_names:
                current_database.execute_sql('ALTER TABLE "msglog" ADD COLUMN "provenance" TEXT NOT NULL DEFAULT \'live\'')

    def _observe_legacy_outbound_rows(self) -> None:
        """Report retained workflow rows without loading or changing them."""
        table_names = set(database.get_tables())
        workflow_table, task_table = self._LEGACY_OUTBOUND_TABLES
        workflow_count = 0
        task_count = 0
        state_counts = {state: 0 for state in self._LEGACY_OUTBOUND_STATES}

        if workflow_table in table_names:
            workflow_count = int(database.execute_sql(f'SELECT COUNT(*) FROM "{workflow_table}"').fetchone()[0])
        if task_table in table_names:
            task_rows = database.execute_sql(f'SELECT state, COUNT(*) FROM "{task_table}" GROUP BY state').fetchall()
            for state, count in task_rows:
                if state in state_counts:
                    state_counts[state] = int(count)
            task_count = sum(int(count) for _state, count in task_rows)

        if workflow_count or task_count:
            state_summary = ", ".join(f"{state}={state_counts[state]}" for state in self._LEGACY_OUTBOUND_STATES)
            self.logger.warning(
                "Retained legacy outbound rows: workflows=%d tasks=%d %s",
                workflow_count,
                task_count,
                state_summary,
            )

    @observe_database_method("add_chat_assoc")
    def add_chat_assoc(self, master_uid: EFBChannelChatIDStr, slave_uid: EFBChannelChatIDStr, multiple_slave: bool = False):
        """
        Add chat associations (chat links).
        One Master channel with many Slave channel.

        Args:
            master_uid (str): Master chat UID ("%(chat_id)s")
            slave_uid (str): Slave channel UID ("%(channel_id)s.%(chat_id)s")
            multiple_slave: Allow linking to multiple slave channels.
        """
        if not multiple_slave:
            self.remove_chat_assoc(master_uid=master_uid)
        self.remove_chat_assoc(slave_uid=slave_uid)
        return ChatAssoc.create(master_uid=master_uid, slave_uid=slave_uid)

    @observe_database_method("remove_chat_assoc")
    def remove_chat_assoc(self, master_uid: Optional[EFBChannelChatIDStr] = None, slave_uid: Optional[EFBChannelChatIDStr] = None):
        """
        Remove chat associations (chat links).
        Only one parameter is to be provided.

        Args:
            master_uid (str): Master chat UID ("%(chat_id)s")
            slave_uid (str): Slave channel UID ("%(channel_id)s.%(chat_id)s")
        """
        try:
            if bool(master_uid) == bool(slave_uid):
                raise ValueError("Only one parameter is to be provided.")
            elif master_uid:
                slave_uids = [row.slave_uid for row in ChatAssoc.select(ChatAssoc.slave_uid).where(ChatAssoc.master_uid == master_uid)]
                result = ChatAssoc.delete().where(ChatAssoc.master_uid == master_uid).execute()
                if slave_uids:
                    TopicAssoc.delete().where(TopicAssoc.slave_uid.in_(slave_uids)).execute()
                return result
            elif slave_uid:
                result = ChatAssoc.delete().where(ChatAssoc.slave_uid == slave_uid).execute()
                TopicAssoc.delete().where(TopicAssoc.slave_uid == slave_uid).execute()
                return result
        except DoesNotExist:
            return 0

    @observe_database_method("get_master_msg_id")
    def get_master_msg_id(self, message: EFBMessage) -> Optional[TgChatMsgIDStr]:
        """Get master message ID from a message object."""
        log: Optional[MsgLog] = MsgLog.get_or_none(MsgLog.slave_origin_uid == chat_id_to_str(chat=message.chat), MsgLog.slave_message_id == message.uid)
        if log:
            return TgChatMsgIDStr(log.master_msg_id)
        return None

    def pickle_misc_msg(self, message: EFBMessage) -> Optional[bytes]:
        """Pickle miscellaneous information of a message.

        Since 2.0.0b34, this would be a dict that reflects the following
        attributes of an ``EFBMessage``/``ETMMsg`` object.

        - ``target``: ``master_msg_id`` of the target message
        - ``is_system``
        - ``attributes``
        - ``commands``
        - ``substitutions``: ``Dict[Tuple[int, int], SlaveChatID]``
        - ``reactions``: ``Dict[str, Collection[SlaveChatID]]``
        """

        data: PickledDict = {}
        if message.is_system:
            data["is_system"] = message.is_system
        if message.attributes:
            data["attributes"] = message.attributes
        if message.commands:
            data["commands"] = message.commands
        if message.substitutions:
            data["substitutions"] = {k: chat_id_to_str(chat=v) for k, v in message.substitutions.items()}
        if message.reactions:
            data["reactions"] = {k: tuple(chat_id_to_str(chat=i) for i in v) for k, v in message.reactions.items()}
        if message.target:
            target_id = self.get_master_msg_id(message.target)
            if target_id:
                data["target"] = target_id

        if data:
            return pickle.dumps(data)
        return None

    @observe_database_method("get_chat_assoc")
    def get_chat_assoc(self, master_uid: Optional[EFBChannelChatIDStr] = None, slave_uid: Optional[EFBChannelChatIDStr] = None) -> List[EFBChannelChatIDStr]:
        """
        Get chat association (chat link) information.
        Only one parameter is to be provided.

        Args:
            master_uid (str): Master channel UID ("%(chat_id)s")
            slave_uid (str): Slave channel UID ("%(channel_id)s.%(chat_id)s")

        Returns:
            list: The counterpart ID.
        """
        try:
            if bool(master_uid) == bool(slave_uid):
                raise ValueError("Only one parameter is to be provided.")
            elif master_uid:
                slaves = list(ChatAssoc.select(ChatAssoc.slave_uid, ChatAssoc.master_uid).where(ChatAssoc.master_uid == master_uid))
                return [EFBChannelChatIDStr(i.slave_uid) for i in slaves]
            elif slave_uid:
                masters = list(ChatAssoc.select(ChatAssoc.slave_uid, ChatAssoc.master_uid).where(ChatAssoc.slave_uid == slave_uid))
                return [EFBChannelChatIDStr(i.master_uid) for i in masters]
            else:
                return []
        except DoesNotExist:
            return []

    @observe_database_method("add_topic_assoc")
    def add_topic_assoc(
        self,
        topic_chat_id: TelegramChatID,
        message_thread_id: TelegramTopicID,
        slave_uid: EFBChannelChatIDStr,
    ):
        """
        Add topic associations (topic links).
        One Master channel with many Slave channel.

        Args:
            topic_chat_id (TelegramChatID): The topic group chat ID
            message_thread_id (EFBChannelChatIDStr): The topic thread ID
            slave_uid (EFBChannelChatIDStr): Slave channel UID ("%(channel_id)s.%(chat_id)s")
        """
        self.remove_topic_assoc(slave_uid=slave_uid)
        self.remove_topic_assoc(topic_chat_id=topic_chat_id, message_thread_id=TelegramTopicID(int(message_thread_id)))
        return TopicAssoc.create(topic_chat_id=topic_chat_id, message_thread_id=message_thread_id, slave_uid=slave_uid)

    @observe_database_method("get_topic_thread_id")
    def get_topic_thread_id(self, slave_uid: EFBChannelChatIDStr, topic_chat_id: Optional[TelegramChatID] = None) -> Optional[TelegramTopicID]:
        """
        Get topic association (topic link) information.
        Only one parameter is to be provided.

        Args:
            topic_chat_id (TelegramChatID): The topic UID
            slave_uid (EFBChannelChatIDStr): Slave channel UID ("%(channel_id)s.%(chat_id)s")

        Returns:
            The message thread_id
        """
        try:
            if topic_chat_id:
                assoc = (
                    TopicAssoc.select(TopicAssoc.message_thread_id)
                    .where(TopicAssoc.slave_uid == slave_uid, TopicAssoc.topic_chat_id == topic_chat_id)
                    .order_by(TopicAssoc.topic_chat_id.desc())
                    .first()
                )
            else:
                assoc = TopicAssoc.select(TopicAssoc.message_thread_id).where(TopicAssoc.slave_uid == slave_uid).order_by(TopicAssoc.topic_chat_id.desc()).first()
            if assoc:
                return TelegramTopicID(int(assoc.message_thread_id))
        except DoesNotExist:
            pass
        return None

    @observe_database_method("get_topic_slave")
    def get_topic_slave(
        self,
        topic_chat_id: TelegramChatID,
        message_thread_id: Optional[TelegramTopicID] = None,
    ) -> Optional[EFBChannelChatIDStr]:
        """
        Get topic association (topic link) information.
        Only one parameter is to be provided.

        Args:
            topic_chat_id (TelegramChatID): The topic chat UID
            message_thread_id (TelegramTopicID): The message thread ID

        Returns:
            Slave channel UID ("%(channel_id)s.%(chat_id)s")
        """
        try:
            if message_thread_id:
                return TopicAssoc.select(TopicAssoc.slave_uid).where(TopicAssoc.message_thread_id == message_thread_id, TopicAssoc.topic_chat_id == topic_chat_id).first().slave_uid
            else:
                return TopicAssoc.select(TopicAssoc.slave_uid).where(TopicAssoc.topic_chat_id == topic_chat_id).first().slave_uid
        except DoesNotExist:
            return None
        except AttributeError:
            return None

    def get_topic_assoc_slave_uid(
        self,
        source_chat_id: int,
        topic_id: int,
    ) -> Optional[EFBChannelChatIDStr]:
        """Return the slave chat bound to one source forum topic."""
        assoc = TopicAssoc.get_or_none((TopicAssoc.topic_chat_id == str(source_chat_id)) & (TopicAssoc.message_thread_id == str(topic_id)))
        return EFBChannelChatIDStr(assoc.slave_uid) if assoc is not None else None

    def get_or_create_msglog_ingestion_scan(
        self,
        source_chat_id: int,
        scan_boundary: int,
    ) -> MsgLogIngestionScan:
        """Return the durable scan for one source group without changing its boundary."""
        if scan_boundary <= 0:
            raise ValueError("scan boundary must be positive")
        source_id = str(source_chat_id)
        scan = MsgLogIngestionScan.get_or_none(MsgLogIngestionScan.source_chat_id == source_id)
        if scan is not None:
            return scan
        try:
            return MsgLogIngestionScan.create(
                source_chat_id=source_id,
                scan_boundary=scan_boundary,
                cursor=scan_boundary,
            )
        except IntegrityError:
            return MsgLogIngestionScan.get(MsgLogIngestionScan.source_chat_id == source_id)

    def claim_msglog_ingestion_scan(
        self,
        source_chat_id: int,
        lease_owner: str,
        lease_seconds: int,
    ) -> Optional[MsgLogIngestionScan]:
        """Claim a scan when no other unexpired worker owns its lease."""
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        now = datetime.datetime.now()
        lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
        with database.atomic():
            updated = (
                MsgLogIngestionScan.update(
                    lease_owner=lease_owner,
                    lease_expires_at=lease_expires_at,
                    status="running",
                    error=None,
                    updated_at=now,
                )
                .where(
                    (MsgLogIngestionScan.source_chat_id == str(source_chat_id))
                    & (MsgLogIngestionScan.status != "complete")
                    & (MsgLogIngestionScan.lease_expires_at.is_null(True) | (MsgLogIngestionScan.lease_expires_at <= now) | (MsgLogIngestionScan.lease_owner == lease_owner))
                )
                .execute()
            )
            if updated != 1:
                return None
            return MsgLogIngestionScan.get(MsgLogIngestionScan.source_chat_id == str(source_chat_id))

    def persist_msglog_ingestion_item(
        self,
        scan: MsgLogIngestionScan,
        *,
        source_message_id: int,
        classification: str,
        slave_uid: Optional[EFBChannelChatIDStr] = None,
        message: Optional[object] = None,
        lease_owner: str,
    ) -> str:
        """Store one scan outcome and its cursor atomically."""
        now = datetime.datetime.now()
        supports_for_update = bool(getattr(database.obj, "for_update", False))
        transaction = database.atomic() if supports_for_update else database.atomic("IMMEDIATE")
        with transaction:
            query = MsgLogIngestionScan.select().where(MsgLogIngestionScan.id == scan.id)
            if supports_for_update:
                query = query.for_update()
            current = query.get()
            if current.lease_owner != lease_owner or (current.lease_expires_at is not None and current.lease_expires_at < now):
                raise MsgLogIngestionLeaseLostError("MsgLog ingestion lease is no longer owned by this worker")
            if current.status == "complete":
                return "complete"

            current.cursor = source_message_id - 1
            current.scanned_count += 1
            if classification != "eligible":
                current.skipped_count += 1
                outcome = "skipped"
            else:
                if slave_uid is None or message is None:
                    raise ValueError("eligible ingestion record is missing its topic association or content")
                master_msg_id = f"{current.source_chat_id}.{source_message_id}"
                existing = MsgLog.get_or_none(MsgLog.master_msg_id == master_msg_id)
                if existing is not None:
                    current.existing_count += 1
                    current.existing_streak += 1
                    outcome = "existing"
                else:
                    slave_channel_id, _, _ = chat_id_str_to_id(slave_uid)
                    synthetic_member_uid = chat_id_to_str(slave_channel_id, ChatID("__self__"))
                    source_time = getattr(message, "time", None)
                    MsgLog.create(
                        master_msg_id=master_msg_id,
                        slave_message_id=f"mtproto-ingested:{master_msg_id}",
                        text=str(getattr(message, "text")),
                        slave_origin_uid=str(slave_uid),
                        slave_member_uid=str(synthetic_member_uid),
                        media_type=str(getattr(message, "media_type")),
                        mime=getattr(message, "mime"),
                        msg_type=str(getattr(message, "msg_type")),
                        sent_to=self.channel.channel_id,
                        provenance="mtproto_ingested",
                        time=source_time if isinstance(source_time, datetime.datetime) else now,
                    )
                    current.inserted_count += 1
                    current.existing_streak = 0
                    outcome = "inserted"
            if current.cursor <= 0 or current.existing_streak >= 500:
                current.status = "complete"
                current.lease_owner = None
                current.lease_expires_at = None
            current.updated_at = now
            current.save()
            scan.__data__.update(current.__data__)
            return outcome

    def finish_msglog_ingestion_scan(
        self,
        scan: MsgLogIngestionScan,
        *,
        status: str,
        error: Optional[str] = None,
        lease_owner: str,
    ) -> bool:
        """Record a terminal or retryable scan state and release its lease."""
        now = datetime.datetime.now()
        with database.atomic():
            updated = (
                MsgLogIngestionScan.update(
                    status=status,
                    error=error,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
                .where(
                    (MsgLogIngestionScan.id == scan.id)
                    & (MsgLogIngestionScan.lease_owner == lease_owner)
                    & MsgLogIngestionScan.lease_expires_at.is_null(False)
                    & (MsgLogIngestionScan.lease_expires_at > now)
                )
                .execute()
            )
            current = MsgLogIngestionScan.get_by_id(scan.id)
            if updated == 1 or current.status == "complete":
                scan.__data__.update(current.__data__)
            return updated == 1

    @observe_database_method("get_topic_slaves")
    def get_topic_slaves(self, topic_chat_id: TelegramChatID) -> Optional[List[Tuple[EFBChannelChatIDStr, TelegramTopicID]]]:
        """
        Get topic association (topic link) information.
        Only one parameter is to be provided.

        Args:
            topic_chat_id (TelegramChatID): The topic UID

        Returns:
            List[Tuple[EFBChannelChatIDStr, TelegramTopicID]]: A list of tuples containing slave channel UID and message thread ID
        """
        try:
            query = TopicAssoc.select(TopicAssoc.slave_uid, TopicAssoc.message_thread_id).where(TopicAssoc.topic_chat_id == topic_chat_id).order_by(getattr(TopicAssoc, "id").desc())
            return [(EFBChannelChatIDStr(row.slave_uid), TelegramTopicID(int(row.message_thread_id))) for row in query]
        except DoesNotExist:
            return None
        except AttributeError:
            return None

    @observe_database_method("remove_topic_assoc")
    def remove_topic_assoc(self, topic_chat_id: Optional[TelegramChatID] = None, message_thread_id: Optional[TelegramTopicID] = None, slave_uid: Optional[EFBChannelChatIDStr] = None):
        """
        Remove topic association (topic link).

        Args:
            topic_chat_id (TelegramChatID): The topic group chat ID
            message_thread_id (EFBChannelChatIDStr): The topic thread ID
            slave_uid (EFBChannelChatIDStr): Slave channel UID ("%(channel_id)s.%(chat_id)s")
        """
        try:
            if bool(topic_chat_id and message_thread_id) == bool(slave_uid):
                raise ValueError("Please provide either topic_chat_id and message_thread_id or slave_uid.")
            elif topic_chat_id and message_thread_id:
                return TopicAssoc.delete().where((TopicAssoc.topic_chat_id == str(topic_chat_id)) & (TopicAssoc.message_thread_id == str(message_thread_id))).execute()
            elif slave_uid:
                return TopicAssoc.delete().where(TopicAssoc.slave_uid == slave_uid).execute()
        except DoesNotExist:
            return 0

    @observe_database_method("add_or_update_message_log")
    def add_or_update_message_log(self, msg: ETMMsg, master_message: Message, old_message_id: Optional[OldMsgID] = None, sender_bot_id: Optional[str] = None):
        """Add or update a message into the database."""
        sent_message_id = message_id_to_str(TelegramChatID(master_message.chat_id), TelegramMessageID(master_message.message_id))
        master_msg_id = sent_message_id
        master_msg_id_alt = None
        self.logger.debug("[%s] Received message logging request of %s", master_msg_id, msg.uid)

        row: Optional[MsgLog] = None
        if old_message_id is not None:
            old_message_id_str = message_id_to_str(*old_message_id)
            row = MsgLog.get_or_none((MsgLog.master_msg_id == old_message_id_str) | (MsgLog.master_msg_id_alt == old_message_id_str))
            if row is not None:
                master_msg_id = TgChatMsgIDStr(row.master_msg_id)
                master_msg_id_alt = sent_message_id if sent_message_id != master_msg_id else row.master_msg_id_alt
            elif sent_message_id != old_message_id_str:
                self.logger.debug("[%s] Message has an old ID: %s", sent_message_id, old_message_id_str)
                master_msg_id, master_msg_id_alt = old_message_id_str, sent_message_id

        if row is None:
            row = MsgLog.get_or_none(MsgLog.master_msg_id == master_msg_id)
        if row is not None:
            save = row.save
            self.logger.debug("[%s] Message record is found in database, update it", master_msg_id)
        else:
            row = MsgLog()
            save = partial(row.save, force_insert=True)
            self.logger.debug("[%s] Message record is not found in database, insert it", master_msg_id)

        row.master_msg_id = master_msg_id
        row.master_msg_id_alt = master_msg_id_alt
        row.text = msg.text
        row.slave_origin_uid = chat_id_to_str(chat=msg.chat)
        row.slave_member_uid = chat_id_to_str(chat=msg.author)
        row.msg_type = msg.type.name
        row.sent_to = msg.deliver_to.channel_id
        row.slave_message_id = msg.uid or f"{self.FAIL_FLAG}.{time.time()}"
        row.media_type = msg.type_telegram.value
        row.file_id = msg.file_id
        row.file_unique_id = msg.file_unique_id
        row.mime = msg.mime
        row.sender_bot_id = sender_bot_id or getattr(msg, "sender_bot_id", None)
        row.provenance = "live"
        pickle_data = self.pickle_misc_msg(msg)
        row.pickle = pickle_data

        result = save()
        self.logger.debug("[%s] Database insert/update outcome: %s", master_msg_id, result)

    @observe_database_method("get_msg_log")
    def get_msg_log(self, master_msg_id: Optional[TgChatMsgIDStr] = None, slave_msg_id: Optional[MessageID] = None, slave_origin_uid: Optional[EFBChannelChatIDStr] = None) -> Optional[MsgLog]:
        """Get message log by message ID.

        Args:
            master_msg_id: Telegram message ID in string
            slave_msg_id: Slave message identifier in string
            slave_origin_uid: Slave chat identifier in string

        Returns:
            Optional[MsgLog]: The queried entry, None if not exist.
        """
        if (master_msg_id and (slave_msg_id or slave_origin_uid)) or not (master_msg_id or (slave_msg_id or slave_origin_uid)):
            raise ValueError("master_msg_id and slave_msg_id is mutual exclusive")
        if not master_msg_id and not (slave_msg_id and slave_origin_uid):
            raise ValueError("slave_msg_id and slave_origin_uid must exists together.")
        try:
            if master_msg_id:
                return MsgLog.select().where(MsgLog.master_msg_id == master_msg_id).order_by(MsgLog.time.desc()).first()
            else:
                return MsgLog.select().where((MsgLog.slave_message_id == slave_msg_id) & (MsgLog.slave_origin_uid == slave_origin_uid)).order_by(MsgLog.time.desc()).first()
        except DoesNotExist:
            return None

    @observe_database_method("delete_msg_log")
    def delete_msg_log(self, master_msg_id: Optional[TgChatMsgIDStr] = None, slave_msg_id: Optional[EFBChannelChatIDStr] = None, slave_origin_uid: Optional[EFBChannelChatIDStr] = None):
        """Remove a message log by message ID.

        Args:
            master_msg_id: Telegram message ID in string
            slave_msg_id: Slave message identifier in string
            slave_origin_uid: Slave chat identifier in string
        """
        if (master_msg_id and (slave_msg_id or slave_origin_uid)) or not (master_msg_id or (slave_msg_id or slave_origin_uid)):
            raise ValueError("master_msg_id and slave_msg_id is mutual exclusive")
        if not master_msg_id and not (slave_msg_id and slave_origin_uid):
            raise ValueError("slave_msg_id and slave_origin_uid must exists together.")
        try:
            if master_msg_id:
                MsgLog.delete().where(MsgLog.master_msg_id == master_msg_id).execute()
            else:
                MsgLog.delete().where((MsgLog.slave_message_id == slave_msg_id) & (MsgLog.slave_origin_uid == slave_origin_uid)).execute()
        except DoesNotExist:
            return

    @observe_database_method("get_slave_chat_info")
    def get_slave_chat_info(self, slave_channel_id: Optional[ModuleID] = None, slave_chat_uid: Optional[ChatID] = None, slave_chat_group_id: Optional[ChatID] = None) -> Optional[SlaveChatInfo]:
        """
        Get cached slave chat info from database.

        Returns:
            SlaveChatInfo|None: The matching slave chat info, None if not exist.
        """
        if slave_channel_id is None or slave_chat_uid is None:
            raise ValueError("Both slave_channel_id and slave_chat_id should be provided.")
        try:
            return (
                SlaveChatInfo.select()
                .where((SlaveChatInfo.slave_channel_id == slave_channel_id) & (SlaveChatInfo.slave_chat_uid == slave_chat_uid) & (SlaveChatInfo.slave_chat_group_id == slave_chat_group_id))
                .first()
            )
        except DoesNotExist:
            return None

    @observe_database_method("set_slave_chat_info")
    def set_slave_chat_info(self, chat_object: "ETMChatMixin") -> SlaveChatInfo:
        """
        Insert or update slave chat info entry

        Args:
            chat_object (ETMChatMixin): Chat object for pickling

        Returns:
            SlaveChatInfo: The inserted or updated row
        """
        slave_channel_id = chat_object.module_id
        slave_channel_emoji = chat_object.channel_emoji
        slave_chat_uid = chat_object.uid
        slave_chat_name = chat_object.name
        slave_chat_alias = chat_object.alias
        slave_chat_type = chat_object.chat_type_name
        parent_chat: Optional["ETMChatMixin"] = getattr(chat_object, "chat", None)
        slave_chat_group_id: Optional[ChatID]
        if parent_chat:
            slave_chat_group_id = parent_chat.uid
        else:
            slave_chat_group_id = None

        chat_info = self.get_slave_chat_info(slave_channel_id=slave_channel_id, slave_chat_uid=slave_chat_uid, slave_chat_group_id=slave_chat_group_id)
        if chat_info is not None:
            chat_info.slave_channel_emoji = slave_channel_emoji
            chat_info.slave_chat_name = slave_chat_name
            chat_info.slave_chat_alias = slave_chat_alias
            chat_info.slave_chat_type = slave_chat_type
            chat_info.pickle = chat_object.pickle
            chat_info.save()
            return chat_info
        else:
            return SlaveChatInfo.create(
                slave_channel_id=slave_channel_id,
                slave_channel_emoji=slave_channel_emoji,
                slave_chat_uid=slave_chat_uid,
                slave_chat_group_id=slave_chat_group_id,
                slave_chat_name=slave_chat_name,
                slave_chat_alias=slave_chat_alias,
                slave_chat_type=slave_chat_type,
                pickle=chat_object.pickle,
            )

    @observe_database_method("delete_slave_chat_info")
    def delete_slave_chat_info(self, slave_channel_id: ModuleID, slave_chat_uid: ChatID, slave_chat_group_id: Optional[ChatID] = None):
        return (
            SlaveChatInfo.delete()
            .where((SlaveChatInfo.slave_channel_id == slave_channel_id) & (SlaveChatInfo.slave_chat_uid == slave_chat_uid) & (SlaveChatInfo.slave_chat_group_id == slave_chat_group_id))
            .execute()
        )

    @observe_database_method("get_recent_slave_chats")
    def get_recent_slave_chats(self, master_chat_id: TelegramChatID, limit=5) -> List[EFBChannelChatIDStr]:
        query = (
            MsgLog.select(MsgLog.slave_origin_uid, fn.MAX(MsgLog.time))
            .where(MsgLog.master_msg_id.startswith("{}.".format(master_chat_id)))
            .group_by(MsgLog.slave_origin_uid)
            .order_by(fn.MAX(MsgLog.time).desc())
            .limit(limit)
        )

        return [EFBChannelChatIDStr(i.slave_origin_uid) for i in query]

    @observe_database_method("get_last_message")
    def get_last_message(self, slave_chat_id: EFBChannelChatIDStr) -> Optional[MsgLog]:
        try:
            return MsgLog.select().where(MsgLog.slave_origin_uid == slave_chat_id).order_by(MsgLog.time.desc()).limit(1).first()
        except DoesNotExist:
            return None

    @observe_database_method("get_recent_messages")
    def get_recent_messages(self, slave_chat_id: EFBChannelChatIDStr, limit: int = 1000) -> List[MsgLog]:
        """Get recent messages from a specific slave chat for migration purposes.

        Args:
            slave_chat_id: Slave chat identifier in string format
            limit: Maximum number of messages to retrieve (default: 1000). Use 0 for no limit.

        Returns:
            List[MsgLog]: List of recent message logs, ordered by time (oldest first)
        """
        try:
            query = MsgLog.select().where(MsgLog.slave_origin_uid == slave_chat_id).order_by(MsgLog.time.asc())

            if limit > 0:
                query = query.limit(limit)

            return list(query)
        except DoesNotExist:
            return []

    @staticmethod
    def _history_migration_target_filter(
        slave_chat_id: EFBChannelChatIDStr,
        target_chat_id: int,
        message_thread_id: Optional[TelegramTopicID] = None,
    ):
        thread_value = str(message_thread_id) if message_thread_id is not None else None
        base_filter = (HistoryMigrationEntry.slave_chat_id == str(slave_chat_id)) & (HistoryMigrationEntry.target_chat_id == str(target_chat_id))
        if thread_value is None:
            return base_filter & HistoryMigrationEntry.message_thread_id.is_null(True)
        return base_filter & (HistoryMigrationEntry.message_thread_id == thread_value)

    @observe_database_method("replace_history_migration_entries")
    def replace_history_migration_entries(
        self,
        slave_chat_id: EFBChannelChatIDStr,
        target_chat_id: int,
        message_thread_id: Optional[TelegramTopicID],
        entries: List[Dict[str, object]],
    ) -> int:
        target_filter = self._history_migration_target_filter(
            slave_chat_id,
            target_chat_id,
            message_thread_id,
        )
        with database.atomic():
            HistoryMigrationEntry.delete().where(target_filter).execute()
            if entries:
                HistoryMigrationEntry.insert_many(entries).execute()
        return len(entries)

    @observe_database_method("has_pending_history_migrations")
    def has_pending_history_migrations(self) -> bool:
        return HistoryMigrationEntry.select().exists()

    @observe_database_method("get_next_history_migration_target")
    def get_next_history_migration_target(self) -> Optional[HistoryMigrationEntry]:
        return HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.id.asc()).first()

    @observe_database_method("get_history_migration_entries")
    def get_history_migration_entries(
        self,
        slave_chat_id: EFBChannelChatIDStr,
        target_chat_id: int,
        message_thread_id: Optional[TelegramTopicID] = None,
    ) -> List[HistoryMigrationEntry]:
        target_filter = self._history_migration_target_filter(
            slave_chat_id,
            target_chat_id,
            message_thread_id,
        )
        return list(HistoryMigrationEntry.select().where(target_filter).order_by(HistoryMigrationEntry.position.asc(), HistoryMigrationEntry.id.asc()))

    @observe_database_method("delete_history_migration_entry")
    def delete_history_migration_entry(self, entry_id: int) -> int:
        return int(HistoryMigrationEntry.delete().where(HistoryMigrationEntry.id == entry_id).execute())

    @observe_database_method("get_resumable_msglog_ingestion_scans")
    def get_resumable_msglog_ingestion_scans(self) -> List[MsgLogIngestionScan]:
        now = datetime.datetime.now()
        return list(
            MsgLogIngestionScan.select()
            .where(
                MsgLogIngestionScan.status.in_(("pending", "retryable-error"))
                | ((MsgLogIngestionScan.status == "running") & (MsgLogIngestionScan.lease_expires_at.is_null(True) | (MsgLogIngestionScan.lease_expires_at <= now)))
            )
            .order_by(MsgLogIngestionScan.updated_at.asc(), MsgLogIngestionScan.id.asc())
        )

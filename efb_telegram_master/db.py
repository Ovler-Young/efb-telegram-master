# coding=utf-8

import datetime
import logging
import pickle
import time
import tempfile
import uuid
from contextlib import nullcontext, suppress
from functools import wraps
from typing import Callable, Collection, Dict, Iterable, List, Optional, Protocol, Tuple, TYPE_CHECKING

from peewee import (
    AutoField,
    BlobField,
    CharField,
    DatabaseProxy,
    DateTimeField,
    DoesNotExist,
    EnclosedNodeList,
    IntegerField,
    Model,
    PostgresqlDatabase,
    TextField,
    Tuple as SQLTuple,
    fn,
    chunked,
)
from playhouse.migrate import migrate

from .db_runtime import (
    SCHEMA_LOCK, DataDirectoryLock, connection_scope, current_schema, postgresql_database, sqlite_database,
)
from telegram import Message
from typing_extensions import TypedDict

from ehforwarderbot import Message as EFBMessage
from ehforwarderbot import utils, Channel, coordinator, MsgType
from ehforwarderbot.message import Substitutions, MessageCommands, MessageAttribute
from ehforwarderbot.types import ModuleID, ChatID, MessageID, ReactionName
from .chat_object_cache import ChatObjectCacheManager
from .message import ETMMsg
from .msg_type import TGMsgType
from .utils import TelegramChatID, EFBChannelChatIDStr, TgChatMsgIDStr, message_id_to_str, \
    chat_id_to_str, OldMsgID, chat_id_str_to_id, TelegramMessageID, TelegramTopicID

if TYPE_CHECKING:
    from . import TelegramChannel
    from .chat import ETMChatMember, ETMChatType

database = DatabaseProxy()


class DatabaseMetrics(Protocol):
    """Metrics interface injected by the bot manager after construction."""

    def record_database_method_call(self, method: str, seconds: float, outcome: str) -> None:
        ...


def observe_database_method(method: str):
    """Measure one public database operation with a statically bounded method label."""
    def decorate(call: Callable):
        @wraps(call)
        def wrapped(manager: 'DatabaseManager', *args, **kwargs):
            started = time.perf_counter()
            outcome = "success"
            try:
                managed_db = getattr(manager, "_managed_database", None)
                scope = connection_scope(managed_db) if managed_db is not None and method != "stop_worker" else nullcontext()
                with scope:
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

PickledDict = TypedDict('PickledDict', {
    "target": TgChatMsgIDStr,
    "is_system": bool,
    "attributes": MessageAttribute,
    "commands": MessageCommands,
    "substitutions": Dict[Tuple[int, int], EFBChannelChatIDStr],
    "reactions": Dict[ReactionName, Collection[EFBChannelChatIDStr]]
}, total=False)
"""
Dict entries for ``pickle`` field of ``msglog`` log.

- ``target``: ``master_msg_id`` of the target message
- ``is_system``
- ``attributes``
- ``commands``
- ``substitutions``: ``Dict[Tuple[int, int], SlaveChatID]``
- ``reactions``: ``Dict[str, Collection[SlaveChatID]]``
"""


class BaseModel(Model):
    class Meta:
        database = database


class TopicAssoc(BaseModel):
    id = AutoField()
    topic_chat_id = TextField()
    message_thread_id = TextField()
    slave_uid = TextField()


class ChatAssoc(BaseModel):
    master_uid = TextField()
    slave_uid = TextField()


class MsgLog(BaseModel):
    master_msg_id = TextField(unique=True, primary_key=True)
    """Message ID from Telegram."""
    master_msg_id_alt = TextField(null=True)
    """Editable message ID from Telegram if ``master_msg_id`` is not editable
    and a separate one is sent.
    """
    slave_message_id = TextField()
    """Message from slave channel."""
    text = TextField()
    """Text in the message."""
    slave_origin_uid = TextField()
    """Channel + chat ID of chat the message is sent to."""
    slave_origin_display_name = TextField(null=True)
    """Deprecated."""
    slave_member_uid = TextField(null=True)
    """Module + chat ID of the user that sent the message in slave channel.
    Can be ``blueset.telegram __self__``."""
    slave_member_display_name = TextField(null=True)
    """Deprecated."""
    media_type = TextField(null=True)
    """Message type in Telegram."""
    mime = TextField(null=True)
    """MIME type of attachment."""
    file_id = TextField(null=True)
    """File ID of attachment in Telegram."""
    file_unique_id = TextField(null=True)
    """Unique file ID of attachment in Telegram."""
    msg_type = TextField()
    """Message type in EFB framework."""
    pickle = BlobField(null=True)
    """Miscellaneous data serialized with ``pickle``, per spec in
    ``DatabaseManager.pickle_misc_msg()``.
    """
    sent_to = TextField()
    """Module ID of the message sent to."""
    master_message_thread_id = TextField(null=True)
    """Telegram topic ID retained from historical message logs."""
    sender_bot_id = TextField(null=True)
    """Telegram bot user ID that sent this message. NULL means the main bot."""
    time = DateTimeField(default=datetime.datetime.now, null=True)
    """Time of the message sent."""

    def build_etm_msg(self, chat_manager: ChatObjectCacheManager,
                      recur: bool = True) -> ETMMsg:
        c_module, c_id, _ = chat_id_str_to_id(EFBChannelChatIDStr(self.slave_origin_uid))
        assert self.slave_member_uid is not None
        a_module, a_id, a_grp = chat_id_str_to_id(EFBChannelChatIDStr(self.slave_member_uid))
        chat: 'ETMChatType' = chat_manager.get_chat(c_module, c_id, build_dummy=True)
        author: 'ETMChatMember' = chat_manager.get_chat_member(a_module, a_grp, a_id, build_dummy=True)  # type: ignore
        msg = ETMMsg(
            uid=MessageID(self.slave_message_id),
            chat=chat,
            author=author,
            text=self.text,
            type=MsgType(self.msg_type),
            type_telegram=TGMsgType(self.media_type),
            mime=self.mime or None,
            file_id=self.file_id or None,
        )
        msg.sender_bot_id = self.sender_bot_id
        with suppress(NameError):
            to_module = coordinator.get_module_by_id(ModuleID(self.sent_to))
            if isinstance(to_module, Channel):
                msg.deliver_to = to_module

        # - ``target``: ``master_msg_id`` of the target message
        # - ``is_system``
        # - ``attributes``
        # - ``commands``
        # - ``substitutions``: ``Dict[Tuple[int, int], SlaveChatID]``
        # - ``reactions``: ``Dict[str, Collection[SlaveChatID]]``
        if self.pickle:
            pickle_data = bytes(self.pickle) if isinstance(self.pickle, memoryview) else self.pickle
            misc_data: PickledDict = pickle.loads(pickle_data)

            if 'target' in misc_data and recur:
                with connection_scope(self._meta.database):
                    target_row = self.get_or_none(MsgLog.master_msg_id == misc_data['target'])
                if target_row:
                    msg.target = target_row.build_etm_msg(chat_manager, recur=False)
            if 'is_system' in misc_data:
                msg.is_system = misc_data['is_system']
            if 'attributes' in misc_data:
                msg.attributes = misc_data['attributes']
            if 'commands' in misc_data:
                msg.commands = misc_data['commands']
            if 'substitutions' in misc_data:
                subs = Substitutions({})
                for sk, sv in misc_data['substitutions'].items():
                    module_id, chat_id, group_id = chat_id_str_to_id(sv)
                    if group_id:
                        subs[sk] = chat_manager.get_chat_member(module_id, group_id, chat_id, build_dummy=True)
                    else:
                        subs[sk] = chat_manager.get_chat(module_id, chat_id, build_dummy=True)
                msg.substitutions = subs
            if 'reactions' in misc_data:
                reactions: Dict[ReactionName, List[ETMChatMember]] = {}
                for rk, rv in misc_data['reactions'].items():
                    reactions[rk] = []
                    for idx in rv:
                        module_id, chat_id, group_id = chat_id_str_to_id(idx)
                        reactions[rk].append(chat_manager.get_chat_member(module_id, group_id, chat_id, build_dummy=True))  # type: ignore
                msg.reactions = reactions
        return msg


class HistoryMigrationEntry(BaseModel):
    id = AutoField()
    slave_chat_id = TextField()
    target_chat_id = TextField()
    message_thread_id = TextField(null=True)
    source_master_msg_id = TextField()
    formatted_text = TextField(null=True)
    media_type = TextField(null=True)
    source_time = DateTimeField(null=True)
    position = IntegerField()
    created_at = DateTimeField(default=datetime.datetime.now)
    generation = TextField(null=True)

    @property
    def ownership_key(self) -> str:
        return f"{self.generation or 'legacy'}:{self.id}"

    class Meta:
        indexes = (
            (("slave_chat_id", "target_chat_id", "message_thread_id", "position"), False),
        )


class HistoryMigrationTarget(BaseModel):
    slave_chat_id = TextField()
    target_chat_id = TextField()
    message_thread_id = TextField(default="")
    generation = TextField()

    class Meta:
        indexes = ((("slave_chat_id", "target_chat_id", "message_thread_id"), True),)


class SlaveChatInfo(BaseModel):
    slave_channel_id = TextField()
    slave_channel_emoji = CharField()
    slave_chat_uid = TextField()
    slave_chat_group_id = TextField(null=True)
    slave_chat_name = TextField()
    slave_chat_alias = TextField(null=True)
    slave_chat_type = CharField()
    pickle = BlobField(null=True)


class DatabaseManager:
    logger = logging.getLogger(__name__)
    FAIL_FLAG = '__fail__'
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

    def __init__(self, channel: 'TelegramChannel'):
        self._metrics: Optional[DatabaseMetrics] = None
        base_path = utils.get_data_path(channel.channel_id)
        self._base_path = base_path

        self.logger.debug("Loading database...")
        db_config = channel.config.get('database', {})
        db_type = db_config.get('type', 'sqlite')

        if db_type == 'postgresql':
            from playhouse.migrate import PostgresqlMigrator
            actual_db = postgresql_database(db_config)
            self._migrator_cls = PostgresqlMigrator
            self._is_sqlite = False
        elif db_type == 'sqlite':
            from playhouse.migrate import SqliteMigrator
            actual_db = sqlite_database(base_path / 'tgdata.db', db_config)
            self._migrator_cls = SqliteMigrator
            self._is_sqlite = True
        else:
            raise ValueError(f"Unknown database type: {db_type!r}")

        self._data_lock = DataDirectoryLock(base_path)
        self._managed_database = actual_db
        database.initialize(actual_db)
        try:
            from .migrate_db import validate_runtime_cutover

            # SQLite must be checked before opening it (opening can create a new file).
            if self._is_sqlite:
                validate_runtime_cutover(base_path, None)
            with connection_scope(actual_db):
                with actual_db.atomic(*(("IMMEDIATE",) if self._is_sqlite else ())):
                    if not self._is_sqlite:
                        actual_db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
                        validate_runtime_cutover(base_path, actual_db)
                    self._create_missing_tables()
                    self._check_and_run_migrations()
                self._reclaim_inactive_history_at_startup()
                self._observe_legacy_outbound_rows()
        except BaseException:
            actual_db.close_all()
            self._data_lock.close()
            raise
        self.logger.debug("Database schema ready; startup never imports SQLite data.")

    def set_metrics(self, metrics: DatabaseMetrics) -> None:
        """Attach the metrics recorder created after the database manager."""
        self._metrics = metrics

    @observe_database_method("stop_worker")
    def stop_worker(self):
        current_db = getattr(self, "_managed_database", database.obj)
        try:
            close_all = getattr(current_db, "close_all", None)
            if callable(close_all):
                close_all()
            else:
                current_db.close()
        finally:
            data_lock = getattr(self, "_data_lock", None)
            if data_lock is not None:
                data_lock.close()

    @staticmethod
    def _create():
        """
        Initializing tables.
        """
        database.create_tables([
            ChatAssoc, MsgLog, SlaveChatInfo, TopicAssoc, HistoryMigrationEntry, HistoryMigrationTarget,
        ])

    @staticmethod
    def _create_missing_tables():
        """Create tables introduced after the original schema without touching existing data."""
        database.create_tables([
            ChatAssoc, MsgLog, SlaveChatInfo, TopicAssoc, HistoryMigrationEntry, HistoryMigrationTarget,
        ], safe=True)

    def _check_and_run_migrations(self):
        """Upgrade missing nullable columns independently, then add lookup indexes.

        The caller holds the schema transaction. Unlike a version cascade, this
        also handles partially upgraded historic databases without duplicate DDL.
        """
        migrator = self._migrator_cls(database.obj)
        schema = current_schema(database.obj)
        for model in (MsgLog, SlaveChatInfo, HistoryMigrationEntry):
            columns = {column.name for column in database.get_columns(model._meta.table_name, schema=schema)}
            for field in model._meta.sorted_fields:
                if field.null and field.column_name not in columns:
                    migrate(migrator.add_column(model._meta.table_name, field.column_name, field))
        self._create_lookup_indexes(database.obj)

    @staticmethod
    def _create_lookup_indexes(db):
        time_order = "time DESC NULLS LAST" if isinstance(db, PostgresqlDatabase) else "time DESC"
        for name, table, columns in (
            ("msglog_slave_lookup", "msglog", f"slave_origin_uid, slave_message_id, {time_order}"),
            ("msglog_chat_time", "msglog", f"slave_origin_uid, {time_order}"),
            ("msglog_history_seek", "msglog", "slave_origin_uid, time, master_msg_id"),
            ("history_generation_id", "historymigrationentry", "generation, id"),
            ("history_target_generation_position", "historymigrationentry", "slave_chat_id, target_chat_id, message_thread_id, generation, position, id"),
            ("history_target_cleanup", "historymigrationentry", "slave_chat_id, target_chat_id, message_thread_id, id"),
            ("msglog_master_alt", "msglog", "master_msg_id_alt"),
            ("chatassoc_slave_lookup", "chatassoc", "slave_uid"),
            ("chatassoc_master_lookup", "chatassoc", "master_uid"),
            ("topicassoc_slave_lookup", "topicassoc", "slave_uid, topic_chat_id"),
            ("topicassoc_topic_lookup", "topicassoc", "topic_chat_id, message_thread_id"),
            ("slavechatinfo_identity_lookup", "slavechatinfo", "slave_channel_id, slave_chat_uid, slave_chat_group_id"),
        ):
            db.execute_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})")

    def _observe_legacy_outbound_rows(self) -> None:
        """Report retained workflow rows without loading or changing them."""
        table_names = set(database.get_tables(schema=current_schema(database.obj)))
        workflow_table, task_table = self._LEGACY_OUTBOUND_TABLES
        workflow_count = 0
        task_count = 0
        state_counts = {state: 0 for state in self._LEGACY_OUTBOUND_STATES}

        if workflow_table in table_names:
            workflow_count = int(
                database.execute_sql(f'SELECT COUNT(*) FROM "{workflow_table}"').fetchone()[0]
            )
        if task_table in table_names:
            task_rows = database.execute_sql(
                f'SELECT state, COUNT(*) FROM "{task_table}" GROUP BY state'
            ).fetchall()
            for state, count in task_rows:
                if state in state_counts:
                    state_counts[state] = int(count)
            task_count = sum(int(count) for _state, count in task_rows)

        if workflow_count or task_count:
            state_summary = ", ".join(
                f"{state}={state_counts[state]}" for state in self._LEGACY_OUTBOUND_STATES
            )
            self.logger.warning(
                "Retained legacy outbound rows: workflows=%d tasks=%d %s",
                workflow_count,
                task_count,
                state_summary,
            )

    @observe_database_method("add_chat_assoc")
    def add_chat_assoc(self, master_uid: EFBChannelChatIDStr,
                       slave_uid: EFBChannelChatIDStr,
                       multiple_slave: bool = False):
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
    def remove_chat_assoc(self, master_uid: Optional[EFBChannelChatIDStr] = None,
                          slave_uid: Optional[EFBChannelChatIDStr] = None):
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
                slave_uids = [
                    row.slave_uid
                    for row in ChatAssoc.select(ChatAssoc.slave_uid).where(ChatAssoc.master_uid == master_uid)
                ]
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
        log: Optional[MsgLog] = MsgLog.get_or_none(
            MsgLog.slave_origin_uid == chat_id_to_str(chat=message.chat),
            MsgLog.slave_message_id == message.uid
        )
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
            data['is_system'] = message.is_system
        if message.attributes:
            data['attributes'] = message.attributes
        if message.commands:
            data['commands'] = message.commands
        if message.substitutions:
            data['substitutions'] = {
                k: chat_id_to_str(chat=v)
                for k, v in message.substitutions.items()
            }
        if message.reactions:
            data['reactions'] = {
                k: tuple(chat_id_to_str(chat=i) for i in v)
                for k, v in message.reactions.items()
            }
        if message.target:
            target_id = self.get_master_msg_id(message.target)
            if target_id:
                data['target'] = target_id

        if data:
            return pickle.dumps(data)
        return None

    @observe_database_method("get_chat_assoc")
    def get_chat_assoc(self, master_uid: Optional[EFBChannelChatIDStr] = None,
                       slave_uid: Optional[EFBChannelChatIDStr] = None
                       ) -> List[EFBChannelChatIDStr]:
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
                slaves = list(
                    ChatAssoc.select(ChatAssoc.slave_uid, ChatAssoc.master_uid)
                    .where(ChatAssoc.master_uid == master_uid)
                )
                return [EFBChannelChatIDStr(i.slave_uid) for i in slaves]
            elif slave_uid:
                masters = list(
                    ChatAssoc.select(ChatAssoc.slave_uid, ChatAssoc.master_uid)
                    .where(ChatAssoc.slave_uid == slave_uid)
                )
                return [EFBChannelChatIDStr(i.master_uid) for i in masters]
            else:
                return []
        except DoesNotExist:
            return []

    @observe_database_method("add_topic_assoc")
    def add_topic_assoc(self, topic_chat_id: TelegramChatID,
                       message_thread_id: TelegramTopicID,
                       slave_uid: EFBChannelChatIDStr, ):
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
                assoc = TopicAssoc.select(TopicAssoc.message_thread_id)\
                    .where(TopicAssoc.slave_uid == slave_uid, TopicAssoc.topic_chat_id == topic_chat_id)\
                    .order_by(TopicAssoc.topic_chat_id.desc()).first()
            else:
                assoc = TopicAssoc.select(TopicAssoc.message_thread_id)\
                    .where(TopicAssoc.slave_uid == slave_uid)\
                    .order_by(TopicAssoc.topic_chat_id.desc()).first()
            if assoc:
                return TelegramTopicID(int(assoc.message_thread_id))
        except DoesNotExist:
            pass
        return None

    @observe_database_method("get_topic_slave")
    def get_topic_slave(self, topic_chat_id: TelegramChatID,
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
                return TopicAssoc.select(TopicAssoc.slave_uid)\
                    .where(TopicAssoc.message_thread_id == message_thread_id, TopicAssoc.topic_chat_id == topic_chat_id).first().slave_uid
            else:
                return TopicAssoc.select(TopicAssoc.slave_uid)\
                    .where(TopicAssoc.topic_chat_id == topic_chat_id).first().slave_uid
        except DoesNotExist:
            return None
        except AttributeError:
            return None

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
            query = TopicAssoc.select(TopicAssoc.slave_uid, TopicAssoc.message_thread_id)\
                .where(TopicAssoc.topic_chat_id == topic_chat_id).order_by(getattr(TopicAssoc, "id").desc())
            return [(EFBChannelChatIDStr(row.slave_uid), TelegramTopicID(int(row.message_thread_id))) for row in query]
        except DoesNotExist:
            return None
        except AttributeError:
            return None

    @observe_database_method("remove_topic_assoc")
    def remove_topic_assoc(self, topic_chat_id: Optional[TelegramChatID] = None,
                           message_thread_id: Optional[TelegramTopicID] = None,
                           slave_uid: Optional[EFBChannelChatIDStr] = None):
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
                return TopicAssoc.delete().where(
                    (TopicAssoc.topic_chat_id == str(topic_chat_id)) &
                    (TopicAssoc.message_thread_id == str(message_thread_id))
                ).execute()
            elif slave_uid:
                return TopicAssoc.delete().where(TopicAssoc.slave_uid == slave_uid).execute()
        except DoesNotExist:
            return 0

    @observe_database_method("add_or_update_message_log")
    def add_or_update_message_log(self,
                                  msg: ETMMsg,
                                  master_message: Message,
                                  old_message_id: Optional[OldMsgID] = None,
                                  sender_bot_id: Optional[str] = None):
        """Add or update a message into the database."""
        sent_message_id = message_id_to_str(
            TelegramChatID(master_message.chat_id), TelegramMessageID(master_message.message_id)
        )
        master_msg_id = sent_message_id
        master_msg_id_alt = None
        self.logger.debug("[%s] Received message logging request of %s", master_msg_id, msg.uid)

        row: Optional[MsgLog] = None
        if old_message_id is not None:
            old_message_id_str = message_id_to_str(*old_message_id)
            row = MsgLog.get_or_none(
                (MsgLog.master_msg_id == old_message_id_str) |
                (MsgLog.master_msg_id_alt == old_message_id_str)
            )
            if row is not None:
                master_msg_id = TgChatMsgIDStr(row.master_msg_id)
                master_msg_id_alt = (
                    sent_message_id if sent_message_id != master_msg_id else row.master_msg_id_alt
                )
            elif sent_message_id != old_message_id_str:
                self.logger.debug("[%s] Message has an old ID: %s", sent_message_id, old_message_id_str)
                master_msg_id, master_msg_id_alt = old_message_id_str, sent_message_id

        if row is None:
            row = MsgLog.get_or_none(MsgLog.master_msg_id == master_msg_id)
        existing = row is not None
        if existing:
            self.logger.debug("[%s] Message record is found in database, update it", master_msg_id)
        else:
            row = MsgLog()
            self.logger.debug("[%s] Message record is not found in database, insert it", master_msg_id)

        assert row is not None
        values = {
            "master_msg_id": master_msg_id,
            "master_msg_id_alt": master_msg_id_alt,
            "text": msg.text,
            "slave_origin_uid": chat_id_to_str(chat=msg.chat),
            "slave_member_uid": chat_id_to_str(chat=msg.author),
            "msg_type": msg.type.name,
            "sent_to": msg.deliver_to.channel_id,
            "slave_message_id": msg.uid or f"{self.FAIL_FLAG}.{time.time()}",
            "media_type": msg.type_telegram.value,
            "file_id": msg.file_id,
            "file_unique_id": msg.file_unique_id,
            "mime": msg.mime,
            "sender_bot_id": sender_bot_id or getattr(msg, 'sender_bot_id', None),
            "pickle": self.pickle_misc_msg(msg),
        }
        if existing:
            changed = {
                name: value for name, value in values.items()
                if getattr(row, name) != value
            }
            for name, value in changed.items():
                setattr(row, name, value)
            if changed:
                result = row.save(only=[MsgLog._meta.fields[name] for name in changed])
            else:
                result = 0
        else:
            for name, value in values.items():
                setattr(row, name, value)
            result = row.save(force_insert=True)
        self.logger.debug("[%s] Database insert/update outcome: %s", master_msg_id, result)

    @observe_database_method("get_msg_log")
    def get_msg_log(self, master_msg_id: Optional[TgChatMsgIDStr] = None,
                    slave_msg_id: Optional[MessageID] = None,
                    slave_origin_uid: Optional[EFBChannelChatIDStr] = None) -> Optional[MsgLog]:
        """Get message log by message ID.

        Args:
            master_msg_id: Telegram message ID in string
            slave_msg_id: Slave message identifier in string
            slave_origin_uid: Slave chat identifier in string

        Returns:
            Optional[MsgLog]: The queried entry, None if not exist.
        """
        if (master_msg_id and (slave_msg_id or slave_origin_uid)) \
                or not (master_msg_id or (slave_msg_id or slave_origin_uid)):
            raise ValueError('master_msg_id and slave_msg_id is mutual exclusive')
        if not master_msg_id and not (slave_msg_id and slave_origin_uid):
            raise ValueError('slave_msg_id and slave_origin_uid must exists together.')
        try:
            if master_msg_id:
                return MsgLog.select().where(MsgLog.master_msg_id == master_msg_id) \
                    .order_by(MsgLog.time.desc(nulls="LAST")).first()
            else:
                return MsgLog.select().where((MsgLog.slave_message_id == slave_msg_id) &
                                             (MsgLog.slave_origin_uid == slave_origin_uid)
                                             ).order_by(MsgLog.time.desc(nulls="LAST")).first()
        except DoesNotExist:
            return None

    @observe_database_method("delete_msg_log")
    def delete_msg_log(self, master_msg_id: Optional[TgChatMsgIDStr] = None,
                       slave_msg_id: Optional[EFBChannelChatIDStr] = None,
                       slave_origin_uid: Optional[EFBChannelChatIDStr] = None):
        """Remove a message log by message ID.

        Args:
            master_msg_id: Telegram message ID in string
            slave_msg_id: Slave message identifier in string
            slave_origin_uid: Slave chat identifier in string
        """
        if (master_msg_id and (slave_msg_id or slave_origin_uid)) \
                or not (master_msg_id or (slave_msg_id or slave_origin_uid)):
            raise ValueError('master_msg_id and slave_msg_id is mutual exclusive')
        if not master_msg_id and not (slave_msg_id and slave_origin_uid):
            raise ValueError('slave_msg_id and slave_origin_uid must exists together.')
        try:
            if master_msg_id:
                MsgLog.delete().where(MsgLog.master_msg_id == master_msg_id).execute()
            else:
                MsgLog.delete().where((MsgLog.slave_message_id == slave_msg_id) &
                                      (MsgLog.slave_origin_uid == slave_origin_uid)
                                      ).execute()
        except DoesNotExist:
            return

    @observe_database_method("get_slave_chat_info")
    def get_slave_chat_info(self, slave_channel_id: Optional[ModuleID] = None,
                            slave_chat_uid: Optional[ChatID] = None,
                            slave_chat_group_id: Optional[ChatID] = None
                            ) -> Optional[SlaveChatInfo]:
        """
        Get cached slave chat info from database.

        Returns:
            SlaveChatInfo|None: The matching slave chat info, None if not exist.
        """
        if slave_channel_id is None or slave_chat_uid is None:
            raise ValueError("Both slave_channel_id and slave_chat_id should be provided.")
        try:
            return SlaveChatInfo.select() \
                .where((SlaveChatInfo.slave_channel_id == slave_channel_id) &
                       (SlaveChatInfo.slave_chat_uid == slave_chat_uid) &
                       (SlaveChatInfo.slave_chat_group_id == slave_chat_group_id)).first()
        except DoesNotExist:
            return None

    @observe_database_method("set_slave_chat_info")
    def set_slave_chat_info(self, chat_object: 'ETMChatType') -> SlaveChatInfo:
        """
        Insert or update slave chat info entry

        Args:
            chat_object (ETMChatType): Chat object for pickling

        Returns:
            SlaveChatInfo: The inserted or updated row
        """
        slave_channel_id = chat_object.module_id
        slave_channel_emoji = chat_object.channel_emoji
        slave_chat_uid = chat_object.uid
        slave_chat_name = chat_object.name
        slave_chat_alias = chat_object.alias
        slave_chat_type = chat_object.chat_type_name
        parent_chat: Optional['ETMChatType'] = getattr(chat_object, 'chat', None)
        slave_chat_group_id: Optional[ChatID]
        if parent_chat:
            slave_chat_group_id = parent_chat.uid
        else:
            slave_chat_group_id = None

        chat_info = self.get_slave_chat_info(slave_channel_id=slave_channel_id,
                                             slave_chat_uid=slave_chat_uid,
                                             slave_chat_group_id=slave_chat_group_id)
        if chat_info is not None:
            chat_info.slave_channel_emoji = slave_channel_emoji
            chat_info.slave_chat_name = slave_chat_name
            chat_info.slave_chat_alias = slave_chat_alias
            chat_info.slave_chat_type = slave_chat_type
            chat_info.pickle = chat_object.pickle
            chat_info.save()
            return chat_info
        else:
            return SlaveChatInfo.create(slave_channel_id=slave_channel_id,
                                        slave_channel_emoji=slave_channel_emoji,
                                        slave_chat_uid=slave_chat_uid,
                                        slave_chat_group_id=slave_chat_group_id,
                                        slave_chat_name=slave_chat_name,
                                        slave_chat_alias=slave_chat_alias,
                                        slave_chat_type=slave_chat_type,
                                        pickle=chat_object.pickle)

    @observe_database_method("delete_slave_chat_info")
    def delete_slave_chat_info(self, slave_channel_id: ModuleID, slave_chat_uid: ChatID, slave_chat_group_id: Optional[ChatID] = None):
        return SlaveChatInfo.delete() \
            .where((SlaveChatInfo.slave_channel_id == slave_channel_id) &
                   (SlaveChatInfo.slave_chat_uid == slave_chat_uid) &
                   (SlaveChatInfo.slave_chat_group_id == slave_chat_group_id)).execute()

    @observe_database_method("get_recent_slave_chats")
    def get_recent_slave_chats(self, master_chat_id: TelegramChatID, limit=5) -> List[EFBChannelChatIDStr]:
        query = MsgLog \
            .select(MsgLog.slave_origin_uid, fn.MAX(MsgLog.time)) \
            .where(MsgLog.master_msg_id.startswith("{}.".format(master_chat_id))) \
            .group_by(MsgLog.slave_origin_uid) \
            .order_by(fn.MAX(MsgLog.time).desc(nulls="LAST")) \
            .limit(limit)

        return [EFBChannelChatIDStr(i.slave_origin_uid) for i in query]

    @observe_database_method("get_last_message")
    def get_last_message(self, slave_chat_id: EFBChannelChatIDStr) -> Optional[MsgLog]:
        try:
            return MsgLog.select().where(
                MsgLog.slave_origin_uid == slave_chat_id
            ).order_by(MsgLog.time.desc(nulls="LAST")).limit(1).first()
        except DoesNotExist:
            return None

    @observe_database_method("get_recent_messages")
    def get_recent_messages(self, slave_chat_id: EFBChannelChatIDStr, limit: int = 1000,
                            after: Optional[Tuple[Optional[datetime.datetime], str]] = None) -> List[MsgLog]:
        """Get recent messages from a specific slave chat for migration purposes.

        Args:
            slave_chat_id: Slave chat identifier in string format
            limit: Maximum number of messages to retrieve (default: 1000). Use 0 for no limit.

        Returns:
            List[MsgLog]: List of recent message logs, ordered by time (oldest first)
        """
        base = MsgLog.select().where(MsgLog.slave_origin_uid == slave_chat_id)
        pages = []
        if after is None or after[0] is None:
            unknown = base.where(MsgLog.time.is_null(True)).order_by(MsgLog.master_msg_id)
            if after is not None:
                unknown = unknown.where(MsgLog.master_msg_id > after[1])
            pages.append(unknown)
        known = base.where(MsgLog.time.is_null(False)).order_by(MsgLog.time, MsgLog.master_msg_id)
        if after is not None and after[0] is not None:
            known = known.where(SQLTuple(MsgLog.time, MsgLog.master_msg_id) > after)
        pages.append(known)
        rows = []
        for query in pages:
            if limit > 0:
                query = query.limit(limit - len(rows))
            rows.extend(query)
            if limit > 0 and len(rows) == limit:
                break
        return rows

    @staticmethod
    def _history_migration_target_filter(
        slave_chat_id: EFBChannelChatIDStr,
        target_chat_id: int,
        message_thread_id: Optional[TelegramTopicID] = None,
    ):
        thread_value = str(message_thread_id) if message_thread_id is not None else None
        base_filter = (
            (HistoryMigrationEntry.slave_chat_id == str(slave_chat_id)) &
            (HistoryMigrationEntry.target_chat_id == str(target_chat_id))
        )
        if thread_value is None:
            return base_filter & HistoryMigrationEntry.message_thread_id.is_null(True)
        return base_filter & (HistoryMigrationEntry.message_thread_id == thread_value)

    @observe_database_method("replace_history_migration_entries")
    def replace_history_migration_entries(
        self,
        slave_chat_id: EFBChannelChatIDStr,
        target_chat_id: int,
        message_thread_id: Optional[TelegramTopicID],
        entries: Iterable[Dict[str, object]],
    ) -> int:
        target_filter = self._history_migration_target_filter(
            slave_chat_id,
            target_chat_id,
            message_thread_id,
        )
        generation = uuid.uuid4().hex
        count = 0
        # Finish the finite read snapshot before taking any main-database write
        # lock. The temporary stream bounds RAM even for million-message chats.
        with tempfile.TemporaryFile() as spool:
            with database.atomic():
                if isinstance(database.obj, PostgresqlDatabase):
                    database.execute_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                for entry in entries:
                    pickle.dump(dict(entry, generation=generation), spool)
                    count += 1
            spool.seek(0)

            def prepared():
                for _ in range(count):
                    yield pickle.load(spool)

            for batch in chunked(prepared(), 32):
                with database.atomic():
                    HistoryMigrationEntry.insert_many(batch).execute()
            # Publishing one pointer makes the entire replacement visible.
            with database.atomic():
                HistoryMigrationTarget.insert(
                    slave_chat_id=str(slave_chat_id), target_chat_id=str(target_chat_id),
                    message_thread_id=str(message_thread_id) if message_thread_id is not None else "",
                    generation=generation,
                ).on_conflict(
                    conflict_target=[HistoryMigrationTarget.slave_chat_id,
                                     HistoryMigrationTarget.target_chat_id,
                                     HistoryMigrationTarget.message_thread_id],
                    update={HistoryMigrationTarget.generation: generation},
                ).execute()
        obsolete = target_filter & (
            HistoryMigrationEntry.generation.is_null(True) | (HistoryMigrationEntry.generation != generation)
        )
        after_id = 0
        while True:
            ids = [row.id for row in HistoryMigrationEntry.select(HistoryMigrationEntry.id)
                   .where(obsolete & (HistoryMigrationEntry.id > after_id))
                   .order_by(HistoryMigrationEntry.id).limit(32)]
            if not ids:
                break
            HistoryMigrationEntry.delete().where(HistoryMigrationEntry.id.in_(ids)).execute()
            after_id = ids[-1]
        return count

    @staticmethod
    def _history_entry_target():
        return HistoryMigrationTarget.select(HistoryMigrationTarget.generation).where(
            (HistoryMigrationTarget.slave_chat_id == HistoryMigrationEntry.slave_chat_id) &
            (HistoryMigrationTarget.target_chat_id == HistoryMigrationEntry.target_chat_id) &
            (HistoryMigrationTarget.message_thread_id == fn.COALESCE(HistoryMigrationEntry.message_thread_id, ""))
        )

    def _reclaim_inactive_history_at_startup(self):
        # The data-directory lock is held and this manager has not been exposed
        # to preparation workers yet. Never run this sweep during preparation.
        after_id = 0
        while True:
            rows = list(HistoryMigrationEntry.select(
                HistoryMigrationEntry.id, HistoryMigrationEntry.generation,
                self._history_entry_target().alias("published_generation"),
            ).where(HistoryMigrationEntry.id > after_id).order_by(HistoryMigrationEntry.id).limit(256).dicts())
            if not rows:
                return
            obsolete = [row["id"] for row in rows if row["generation"] != row["published_generation"]]
            if obsolete:
                HistoryMigrationEntry.delete().where(HistoryMigrationEntry.id.in_(obsolete)).execute()
            after_id = rows[-1]["id"]

    @observe_database_method("has_pending_history_migrations")
    def has_pending_history_migrations(self) -> bool:
        return self.get_next_history_migration_target() is not None

    @observe_database_method("get_next_history_migration_target")
    def get_next_history_migration_target(self) -> Optional[HistoryMigrationEntry]:
        # Seek one head per published generation; ORDER BY id over a combined
        # visibility predicate can instead scan every unpublished staging row.
        head = HistoryMigrationEntry.select(HistoryMigrationEntry.id).where(
            HistoryMigrationEntry.generation == HistoryMigrationTarget.generation,
        ).order_by(HistoryMigrationEntry.id).limit(1)
        published_id = HistoryMigrationTarget.select(fn.MIN(EnclosedNodeList([head]))).scalar()
        legacy_id = HistoryMigrationEntry.select(HistoryMigrationEntry.id).where(
            HistoryMigrationEntry.generation.is_null(True) & ~fn.EXISTS(self._history_entry_target())
        ).order_by(HistoryMigrationEntry.id).limit(1).scalar()
        ids = [identifier for identifier in (published_id, legacy_id) if identifier is not None]
        return HistoryMigrationEntry.get_by_id(min(ids)) if ids else None

    @observe_database_method("get_history_migration_entries")
    def get_history_migration_entries(
        self,
        slave_chat_id: EFBChannelChatIDStr,
        target_chat_id: int,
        message_thread_id: Optional[TelegramTopicID] = None,
        limit: Optional[int] = None,
        after: Optional[Tuple[int, int]] = None,
    ) -> List[HistoryMigrationEntry]:
        target_filter = self._history_migration_target_filter(
            slave_chat_id,
            target_chat_id,
            message_thread_id,
        )
        generation = HistoryMigrationTarget.select(HistoryMigrationTarget.generation).where(
            (HistoryMigrationTarget.slave_chat_id == str(slave_chat_id)) &
            (HistoryMigrationTarget.target_chat_id == str(target_chat_id)) &
            (HistoryMigrationTarget.message_thread_id == (str(message_thread_id) if message_thread_id is not None else ""))
        ).scalar()
        generation_filter = (
            (HistoryMigrationEntry.generation == generation) if generation is not None
            else HistoryMigrationEntry.generation.is_null(True)
        )
        query = (
            HistoryMigrationEntry.select()
            .where(target_filter & generation_filter)
            .order_by(HistoryMigrationEntry.position.asc(), HistoryMigrationEntry.id.asc())
        )
        if after is not None:
            position, identifier = after
            query = query.where(
                SQLTuple(HistoryMigrationEntry.position, HistoryMigrationEntry.id) > (position, identifier)
            )
        if limit is not None:
            query = query.limit(limit)
        return list(query)

    def get_history_migration_ownership_keys(self, entry_ids: Collection[int]) -> List[str]:
        keys = {}
        with connection_scope(self._managed_database):
            for batch in chunked(entry_ids, 100):
                for entry in HistoryMigrationEntry.select(
                    HistoryMigrationEntry.id, HistoryMigrationEntry.generation,
                ).where(HistoryMigrationEntry.id.in_(batch)):
                    keys[entry.id] = entry.ownership_key
        return [keys[identifier] for identifier in entry_ids]

    def existing_history_ownership(self, keys: Collection[str]) -> set[str]:
        ids = [int(key.rsplit(":", 1)[1]) for key in keys]
        with connection_scope(self._managed_database):
            return {entry.ownership_key for entry in HistoryMigrationEntry.select(
                HistoryMigrationEntry.id, HistoryMigrationEntry.generation,
            ).where(HistoryMigrationEntry.id.in_(ids))} & set(keys)

    @observe_database_method("delete_history_migration_entry")
    def delete_history_migration_entry(self, entry_id: int) -> int:
        return int(
            HistoryMigrationEntry.delete()
            .where(HistoryMigrationEntry.id == entry_id)
            .execute()
        )

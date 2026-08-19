# coding=utf-8

import re
from typing import Optional, Tuple

from peewee import Model, PostgresqlDatabase, SqliteDatabase


class LegacyOutboundRetirement:
    """Validate and retire the historical durable outbound tables."""

    TABLES = ("outboundworkflow", "outboundtask")
    _LOCK_KEY = 681_774_240_616_480_003
    _COLUMNS = {
        "outboundworkflow": (
            ("id", "integer", False, True),
            ("state", "text", False, False),
            ("result_task_id", "integer", True, False),
            ("error_class", "text", True, False),
            ("created_at", "datetime", False, False),
            ("completed_at", "datetime", True, False),
        ),
        "outboundtask": (
            ("id", "integer", False, True),
            ("source_key", "text", False, False),
            ("slave_id", "text", True, False),
            ("priority", "boolean", False, False),
            ("target_chat_id", "integer", False, False),
            ("message_thread_id", "integer", True, False),
            ("operation", "text", False, False),
            ("payload", "text", False, False),
            ("media_ref", "text", True, False),
            ("workflow_id", "integer", False, False),
            ("step_index", "integer", False, False),
            ("depends_on_task_id", "integer", True, False),
            ("run_condition", "text", False, False),
            ("result_payload", "text", True, False),
            ("log_payload", "text", True, False),
            ("required_sender_bot_id", "text", True, False),
            ("state", "text", False, False),
            ("available_at", "datetime", True, False),
            ("lease_owner", "text", True, False),
            ("lease_until", "datetime", True, False),
            ("lease_heartbeat_at", "datetime", True, False),
            ("submitted_at", "datetime", True, False),
            ("attempt_count", "integer", False, False),
            ("accepted_at", "datetime", False, False),
            ("error_class", "text", True, False),
            ("last_error", "text", True, False),
        ),
    }
    _TASK_INDEXES = (
        ("outboundtask_workflow_id", ("workflow_id",), False),
        ("outboundtask_source_key_priority_accepted_at_id", ("source_key", "priority", "accepted_at", "id"), False),
        ("outboundtask_state_available_at", ("state", "available_at"), False),
        ("outboundtask_workflow_id_step_index", ("workflow_id", "step_index"), True),
    )
    _DEFAULT_CATEGORIES = {
        "outboundworkflow": {"id": "auto_pk", "created_at": "current_timestamp"},
        "outboundtask": {"id": "auto_pk", "accepted_at": "current_timestamp"},
    }

    def __init__(self, current_database) -> None:
        self._database = current_database

    def reject_source_data(self) -> None:
        self._reject_data("SQLite import source", "Import aborted before target initialization or source migration")

    def reject_target_data(self) -> None:
        self._reject_data("PostgreSQL import target", "Import aborted before target initialization or source migration")

    def retire_tables(self) -> None:
        current_database = self._database
        transaction_arguments: Tuple[str, ...] = ()
        if isinstance(current_database, SqliteDatabase):
            transaction_arguments = ("IMMEDIATE",)
        elif not isinstance(current_database, PostgresqlDatabase):
            raise TypeError(f"Unsupported database backend: {type(current_database).__name__}")
        with current_database.atomic(*transaction_arguments):
            self._acquire_lock(current_database)
            legacy_table_names = self.validate_schema(current_database, set(current_database.get_tables()))
            if not legacy_table_names:
                return
            row_counts = self._row_counts(current_database, legacy_table_names)
            if any(row_counts.values()):
                raise RuntimeError(
                    "Legacy durable outbound data detected: automatic replay is disabled. "
                    "Export or explicitly retire outboundworkflow/outboundtask rows before restarting; "
                    f"workflows={row_counts.get('outboundworkflow', 0)} tasks={row_counts.get('outboundtask', 0)}."
                )
            for table_name in reversed(legacy_table_names):
                current_database.drop_tables([self._table_model(table_name, current_database)], safe=False)

    @classmethod
    def validate_schema(cls, current_database, table_names: set[str]) -> tuple[str, ...]:
        legacy_tables = tuple(table_name for table_name in cls.TABLES if table_name in table_names)
        if legacy_tables and len(legacy_tables) != len(cls.TABLES):
            raise RuntimeError("Legacy outbound partial-schema collision: historical workflow and task tables must be present together; refusing to discard table")
        for table_name in legacy_tables:
            error = cls.schema_error(current_database, table_name)
            if error is not None:
                raise RuntimeError(f"Legacy outbound schema collision: {error}; refusing to discard table")
        return legacy_tables

    @classmethod
    def schema_error(cls, current_database, table_name: str) -> Optional[str]:
        expected_columns = tuple(
            (name, "integer" if isinstance(current_database, SqliteDatabase) and data_type == "boolean" else data_type, null, primary_key, cls._DEFAULT_CATEGORIES[table_name].get(name, "none"))
            for name, data_type, null, primary_key in cls._COLUMNS[table_name]
        )
        actual_columns = tuple(
            (
                column.name,
                cls._column_type(column.data_type),
                column.null,
                column.primary_key,
                cls._default_category(current_database, table_name, column.name, cls._column_type(column.data_type), column.primary_key, column.default),
            )
            for column in current_database.get_columns(table_name)
        )
        if actual_columns != expected_columns:
            return f"column signature for {table_name} does not match the historical durable outbound schema"
        if table_name == "outboundtask" and set(cls.task_indexes(current_database)) != set(cls._TASK_INDEXES):
            return "index signature for outboundtask does not match the historical durable outbound schema"
        return None

    @classmethod
    def task_indexes(cls, current_database) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
        indexes = tuple((index.name, tuple(index.columns), index.unique) for index in current_database.get_indexes("outboundtask"))
        if isinstance(current_database, PostgresqlDatabase):
            indexes = tuple(index for index in indexes if index != ("outboundtask_pkey", ("id",), True))
        return indexes

    def _reject_data(self, location: str, action: str) -> None:
        table_names = self.validate_schema(self._database, set(self._database.get_tables()))
        row_counts = self._row_counts(self._database, table_names)
        if any(row_counts.values()):
            raise RuntimeError(
                f"Legacy durable outbound data detected in {location}: automatic replay is disabled. "
                f"{action}; workflows={row_counts.get('outboundworkflow', 0)} tasks={row_counts.get('outboundtask', 0)}."
            )

    @staticmethod
    def _row_counts(current_database, table_names: tuple[str, ...]) -> dict[str, int]:
        return {table_name: int(current_database.execute_sql(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]) for table_name in table_names}

    @staticmethod
    def _table_model(table_name: str, current_database) -> type[Model]:
        meta = type("Meta", (), {"database": current_database, "table_name": table_name})
        return type("LegacyOutboundTable", (Model,), {"Meta": meta})

    @staticmethod
    def _column_type(data_type: str) -> str:
        normalized = data_type.lower()
        if "int" in normalized or normalized in {"serial", "bigserial"}:
            return "integer"
        if "bool" in normalized:
            return "boolean"
        if "date" in normalized or "time" in normalized:
            return "datetime"
        if "char" in normalized or "text" in normalized:
            return "text"
        return normalized

    @staticmethod
    def _column_default(default) -> Optional[str]:
        if default is None:
            return None
        normalized = str(default).strip().lower()
        while normalized.startswith("(") and normalized.endswith(")"):
            normalized = normalized[1:-1].strip()
        normalized = normalized.replace("::timestamp without time zone", "").replace("::timestamp with time zone", "")
        return normalized.replace("::text", "").strip()

    @staticmethod
    def _auto_pk_default(default) -> str:
        raw_default = str(default).strip().lower()
        while raw_default.startswith("(") and raw_default.endswith(")"):
            raw_default = raw_default[1:-1].strip()
        return raw_default

    @classmethod
    def _auto_pk_sequence(cls, default, table_name: str) -> bool:
        identifier = r'(?:(?P<schema>"[^"]+"|[a-z_][a-z0-9_]*)\s*\.\s*)?(?P<sequence>"[^"]+"|[a-z_][a-z0-9_]*)'
        match = re.fullmatch(rf"nextval\s*\(\s*'{identifier}'\s*::\s*regclass\s*\)", cls._auto_pk_default(default), flags=re.IGNORECASE)
        if match is None:
            return False
        schema = match.group("schema")
        return (schema is None or schema.strip('"').lower() == "public") and match.group("sequence").strip('"').lower() == f"{table_name}_id_seq"

    @classmethod
    def _default_category(cls, current_database, table_name: str, column_name: str, data_type: str, primary_key: bool, default) -> str:
        expected_category = cls._DEFAULT_CATEGORIES[table_name].get(column_name, "none")
        if expected_category == "auto_pk" and column_name == "id" and primary_key and data_type == "integer":
            if default is None:
                return "auto_pk" if isinstance(current_database, SqliteDatabase) else "none"
            if isinstance(current_database, PostgresqlDatabase) and cls._auto_pk_sequence(default, table_name):
                return "auto_pk"
            return f"invalid:{cls._auto_pk_default(default)}"
        normalized_default = cls._column_default(default)
        if normalized_default is None:
            return "none"
        if normalized_default == "current_timestamp":
            return "current_timestamp"
        return f"invalid:{normalized_default}"

    @classmethod
    def _acquire_lock(cls, current_database) -> None:
        if isinstance(current_database, PostgresqlDatabase):
            current_database.execute_sql("SELECT pg_advisory_xact_lock(%s)", (cls._LOCK_KEY,))

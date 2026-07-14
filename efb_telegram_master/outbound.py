# coding=utf-8
"""Durable outbound commands and source-ordered repository operations."""

from __future__ import annotations

import datetime
import enum
import io
import json
import logging
import os
import tempfile
import threading
import uuid
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence

import telegram
from peewee import fn
from telegram import InputFile, TelegramObject

from .db import OutboundTask, OutboundWorkflow
from .rate_limiter import SlotReservation


def utc_now() -> datetime.datetime:
    """Return naive UTC for database fields shared by SQLite and PostgreSQL."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class TaskState:
    WAITING_DEPENDENCY = "waiting_dependency"
    QUEUED = "queued"
    LEASED = "leased"
    IN_FLIGHT = "in_flight"
    SENT_PENDING_LOG = "sent_pending_log"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    DEAD = "dead"

    UNSUBMITTED = (WAITING_DEPENDENCY, QUEUED, LEASED)
    LANE_HEAD_STATES = (WAITING_DEPENDENCY, QUEUED, LEASED)
    ACTIVE = (WAITING_DEPENDENCY, QUEUED, LEASED, IN_FLIGHT, SENT_PENDING_LOG)
    TERMINAL = (COMPLETED, SKIPPED, DEAD)


class RunCondition:
    ALWAYS = "always"
    PREDECESSOR_SUCCESS = "predecessor_success"
    PREDECESSOR_ERROR_PREFIX = "predecessor_error:"


@dataclass(frozen=True)
class OutboundTaskSpec:
    source_key: str
    slave_id: Optional[str]
    priority: bool
    target_chat_id: int
    message_thread_id: Optional[int]
    operation: str
    args: tuple
    kwargs: Mapping[str, object]
    state: str = TaskState.QUEUED
    depends_on_task_id: Optional[int] = None
    depends_on_step_index: Optional[int] = None
    run_condition: str = RunCondition.ALWAYS
    required_sender_bot_id: Optional[str] = None
    log_payload: Optional[Mapping[str, object]] = None
    owned_local_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class EncodedCommand:
    payload: str
    media_refs: tuple[str, ...]


@dataclass(frozen=True)
class CreatedWorkflow:
    workflow: OutboundWorkflow
    tasks: tuple[OutboundTask, ...]


@dataclass(frozen=True)
class RecoveryPlan:
    requeued_ambiguous_ids: tuple[int, ...]
    sent_pending_log_ids: tuple[int, ...]


@dataclass(frozen=True)
class SenderSelection:
    sender: object
    sender_bot_id: Optional[str]
    reservation: SlotReservation


@dataclass(frozen=True)
class SenderSelectionResult:
    selection: Optional[SenderSelection] = None
    retry_at: Optional[datetime.datetime] = None
    reason: str = "unavailable"
    terminal_error_class: Optional[str] = None


@dataclass(frozen=True)
class FailureDisposition:
    kind: str
    error_class: str
    retry_at: Optional[datetime.datetime] = None

    RETRY = "retry"
    EXPECTED = "expected"
    DEAD = "dead"


@dataclass(frozen=True)
class WorkflowOutcome:
    workflow_id: int
    state: str
    result_task_id: Optional[int]
    result_payload: Optional[dict]
    error_class: Optional[str]


class OutboundSchedulerAdapter(Protocol):
    def select_sender(
        self,
        task: OutboundTask,
        now: datetime.datetime,
    ) -> SenderSelectionResult:
        ...

    def execute_task(self, task: OutboundTask, selection: SenderSelection) -> object:
        ...

    def release_reservation(self, selection: SenderSelection) -> None:
        ...

    def serialize_result(
        self,
        task: OutboundTask,
        result: object,
        selection: SenderSelection,
    ) -> Mapping[str, object]:
        ...

    def classify_error(
        self,
        task: OutboundTask,
        error: Exception,
        selection: SenderSelection,
        now: datetime.datetime,
    ) -> FailureDisposition:
        ...

    def reconcile_sent_task(
        self,
        task: OutboundTask,
        result_payload: Mapping[str, object],
        selection: SenderSelection,
    ) -> None:
        ...

    def workflow_finished(self, workflow_outcome: WorkflowOutcome) -> None:
        ...


@dataclass(frozen=True)
class InFlightTask:
    future: Future
    task_id: int
    selection: SenderSelection


class OutboundPayloadCodec:
    """Encode bounded Telegram operations as versioned JSON plus spool files."""

    VERSION = 1
    TYPE_KEY = "__etm_type__"

    def __init__(self, spool_dir: Path):
        self.spool_dir = Path(spool_dir)
        self.spool_dir.mkdir(parents=True, exist_ok=True)

    def encode_command(
        self,
        operation: str,
        args: Sequence[object],
        kwargs: Mapping[str, object],
        *,
        owned_local_paths: Sequence[str] = (),
    ) -> EncodedCommand:
        media_refs: list[str] = []
        managed_paths: dict[str, Path] = {}
        for path_value in owned_local_paths:
            path = Path(path_value).resolve()
            managed_paths[path_value] = path
            managed_paths[str(path)] = path
            managed_paths[path.as_uri()] = path
        document = {
            "version": self.VERSION,
            "operation": operation,
            "args": self._encode(tuple(args), media_refs, managed_paths),
            "kwargs": self._encode(dict(kwargs), media_refs, managed_paths),
        }
        return EncodedCommand(
            payload=json.dumps(document, separators=(",", ":"), sort_keys=True),
            media_refs=tuple(media_refs),
        )

    def decode_command(self, payload: str) -> tuple[str, tuple, dict]:
        document = json.loads(payload)
        if document.get("version") != self.VERSION:
            raise ValueError(f"Unsupported outbound payload version: {document.get('version')!r}")
        operation = document.get("operation")
        if not isinstance(operation, str) or not operation:
            raise ValueError("Outbound payload is missing an operation name.")
        args = self._decode(document.get("args"))
        kwargs = self._decode(document.get("kwargs"))
        if not isinstance(args, tuple) or not isinstance(kwargs, dict):
            raise ValueError("Outbound payload args/kwargs have invalid shapes.")
        return operation, args, kwargs

    def cleanup_media_refs(self, media_refs: Sequence[str]) -> None:
        for media_ref in media_refs:
            try:
                (self.spool_dir / media_ref).unlink()
            except FileNotFoundError:
                continue

    def _spool_bytes(self, content: bytes, filename: Optional[str]) -> str:
        suffix = Path(filename).suffix[:32] if filename else ""
        spool_name = f"{uuid.uuid4().hex}{suffix}"
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.spool_dir, delete=False) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.spool_dir / spool_name)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return spool_name

    @staticmethod
    def _read_file_like(value: object) -> bytes:
        current_position = None
        tell = getattr(value, "tell", None)
        seek = getattr(value, "seek", None)
        read = getattr(value, "read", None)
        if not callable(read):
            raise TypeError(f"Object is not readable: {type(value).__name__}")
        if callable(tell):
            try:
                current_position = tell()
            except (OSError, ValueError):
                pass
        if callable(seek):
            seek(0)
        try:
            content = read()
        finally:
            if current_position is not None and callable(seek):
                try:
                    seek(current_position)
                except (OSError, ValueError):
                    pass
        if isinstance(content, str):
            return content.encode("utf-8")
        if not isinstance(content, bytes):
            content = bytes(content)
        return content

    @staticmethod
    def _managed_local_path(
        value: object,
        managed_paths: Mapping[str, Path],
    ) -> Optional[Path]:
        if not isinstance(value, str) or not managed_paths:
            return None
        return managed_paths.get(value)

    def _encode(
        self,
        value: object,
        media_refs: list[str],
        managed_paths: Mapping[str, Path],
    ) -> object:
        managed_path = self._managed_local_path(value, managed_paths)
        if managed_path is not None:
            media_ref = self._spool_bytes(managed_path.read_bytes(), managed_path.name)
            media_refs.append(media_ref)
            return {self.TYPE_KEY: "file", "media_ref": media_ref, "filename": managed_path.name}
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, enum.Enum):
            return self._encode(value.value, media_refs, managed_paths)
        if isinstance(value, (bytes, bytearray, memoryview)):
            media_ref = self._spool_bytes(bytes(value), None)
            media_refs.append(media_ref)
            return {self.TYPE_KEY: "bytes", "media_ref": media_ref}
        if isinstance(value, InputFile):
            content = value.input_file_content
            if not isinstance(content, bytes):
                content = self._read_file_like(content)
            media_ref = self._spool_bytes(content, value.filename)
            media_refs.append(media_ref)
            return {
                self.TYPE_KEY: "input_file",
                "media_ref": media_ref,
                "filename": value.filename,
                "attach": value.attach_name is not None,
            }
        if callable(getattr(value, "read", None)):
            filename = getattr(value, "name", None)
            filename = os.path.basename(filename) if isinstance(filename, str) else None
            media_ref = self._spool_bytes(self._read_file_like(value), filename)
            media_refs.append(media_ref)
            return {self.TYPE_KEY: "file", "media_ref": media_ref, "filename": filename}
        if isinstance(value, TelegramObject):
            return {
                self.TYPE_KEY: "telegram_object",
                "class": value.__class__.__name__,
                "data": self._encode(value.to_dict(recursive=False), media_refs, managed_paths),
            }
        if isinstance(value, datetime.datetime):
            return {self.TYPE_KEY: "datetime", "value": value.isoformat()}
        if isinstance(value, datetime.date):
            return {self.TYPE_KEY: "date", "value": value.isoformat()}
        if isinstance(value, datetime.timedelta):
            return {self.TYPE_KEY: "timedelta", "seconds": value.total_seconds()}
        if isinstance(value, tuple):
            return {
                self.TYPE_KEY: "tuple",
                "items": [self._encode(item, media_refs, managed_paths) for item in value],
            }
        if isinstance(value, list):
            return [self._encode(item, media_refs, managed_paths) for item in value]
        if isinstance(value, Mapping):
            if all(isinstance(key, str) for key in value):
                if self.TYPE_KEY in value:
                    raise TypeError(f"Mapping key {self.TYPE_KEY!r} is reserved by the outbound codec.")
                return {
                    key: self._encode(item, media_refs, managed_paths)
                    for key, item in value.items()
                }
            return {
                self.TYPE_KEY: "mapping",
                "items": [
                    [
                        self._encode(key, media_refs, managed_paths),
                        self._encode(item, media_refs, managed_paths),
                    ]
                    for key, item in value.items()
                ],
            }
        raise TypeError(f"Outbound payload value is not JSON serializable: {type(value).__name__}")

    def _decode(self, value: object) -> object:
        if isinstance(value, list):
            return [self._decode(item) for item in value]
        if not isinstance(value, dict):
            return value
        value_type = value.get(self.TYPE_KEY)
        if value_type is None:
            return {key: self._decode(item) for key, item in value.items()}
        if value_type in {"bytes", "file", "input_file"}:
            media_ref = value["media_ref"]
            if not isinstance(media_ref, str):
                raise ValueError("Outbound media_ref must be a string.")
            content = (self.spool_dir / media_ref).read_bytes()
            if value_type == "bytes":
                return content
            if value_type == "file":
                stream = io.BytesIO(content)
                filename = value.get("filename")
                if isinstance(filename, str):
                    stream.name = filename  # type: ignore[attr-defined]
                return stream
            return InputFile(
                content,
                filename=value.get("filename"),
                attach=bool(value.get("attach")),
            )
        if value_type == "telegram_object":
            class_name = value.get("class")
            telegram_class = getattr(telegram, class_name, None) if isinstance(class_name, str) else None
            if telegram_class is None or not issubclass(telegram_class, TelegramObject):
                raise ValueError(f"Unsupported Telegram object class: {class_name!r}")
            data = self._decode(value.get("data"))
            if not isinstance(data, dict):
                raise ValueError("Telegram object payload must decode to a mapping.")
            return telegram_class.de_json(data, None)
        if value_type == "datetime":
            return datetime.datetime.fromisoformat(str(value["value"]))
        if value_type == "date":
            return datetime.date.fromisoformat(str(value["value"]))
        if value_type == "timedelta":
            return datetime.timedelta(seconds=float(value["seconds"]))
        if value_type == "tuple":
            return tuple(self._decode(item) for item in value.get("items", ()))
        if value_type == "mapping":
            return {
                self._decode(pair[0]): self._decode(pair[1])
                for pair in value.get("items", ())
            }
        raise ValueError(f"Unsupported outbound payload value type: {value_type!r}")


class OutboundRepository:
    """Database operations that define durable source-lane ordering."""

    def __init__(self, codec: OutboundPayloadCodec):
        self.codec = codec

    def create_workflow(
        self,
        specs: Sequence[OutboundTaskSpec],
        *,
        result_task_index: int = 0,
    ) -> CreatedWorkflow:
        if not specs:
            raise ValueError("An outbound workflow requires at least one task.")
        if result_task_index < 0 or result_task_index >= len(specs):
            raise ValueError("result_task_index is outside the workflow task list.")

        encoded_specs: list[tuple[OutboundTaskSpec, EncodedCommand]] = []
        all_media_refs: list[str] = []
        try:
            for spec in specs:
                encoded = self.codec.encode_command(
                    spec.operation,
                    spec.args,
                    spec.kwargs,
                    owned_local_paths=spec.owned_local_paths,
                )
                encoded_specs.append((spec, encoded))
                all_media_refs.extend(encoded.media_refs)

            with OutboundTask._meta.database.atomic():
                workflow = OutboundWorkflow.create()
                tasks: list[OutboundTask] = []
                for step_index, (spec, encoded) in enumerate(encoded_specs):
                    dependency_id = spec.depends_on_task_id
                    if spec.depends_on_step_index is not None:
                        if spec.depends_on_step_index >= len(tasks):
                            raise ValueError("Workflow dependencies must reference an earlier step.")
                        dependency_id = tasks[spec.depends_on_step_index].id
                    state = spec.state
                    if dependency_id is not None and state == TaskState.QUEUED:
                        state = TaskState.WAITING_DEPENDENCY
                    task = OutboundTask.create(
                        source_key=spec.source_key,
                        slave_id=spec.slave_id,
                        priority=spec.priority,
                        target_chat_id=spec.target_chat_id,
                        message_thread_id=spec.message_thread_id,
                        operation=spec.operation,
                        payload=encoded.payload,
                        media_ref=json.dumps(encoded.media_refs),
                        workflow_id=workflow.id,
                        step_index=step_index,
                        depends_on_task_id=dependency_id,
                        run_condition=spec.run_condition,
                        log_payload=(
                            json.dumps(spec.log_payload, separators=(",", ":"), sort_keys=True)
                            if spec.log_payload is not None else None
                        ),
                        required_sender_bot_id=spec.required_sender_bot_id,
                        state=state,
                    )
                    tasks.append(OutboundTask.get_by_id(task.id))
                workflow.result_task_id = tasks[result_task_index].id
                workflow.save(only=[OutboundWorkflow.result_task_id])
            return CreatedWorkflow(
                workflow=OutboundWorkflow.get_by_id(workflow.id),
                tasks=tuple(tasks),
            )
        except Exception:
            self.codec.cleanup_media_refs(all_media_refs)
            raise

    @staticmethod
    def list_lane_heads(now: datetime.datetime) -> list[OutboundTask]:
        candidate = OutboundTask.alias("candidate")
        earlier = OutboundTask.alias("earlier")
        earlier_in_lane = (
            (earlier.priority > candidate.priority)
            | (
                (earlier.priority == candidate.priority)
                & (
                    (earlier.accepted_at < candidate.accepted_at)
                    | (
                        (earlier.accepted_at == candidate.accepted_at)
                        & (earlier.id < candidate.id)
                    )
                )
            )
        )
        earlier_query = earlier.select(earlier.id).where(
            earlier.source_key == candidate.source_key,
            earlier.state.in_(TaskState.LANE_HEAD_STATES),
            earlier_in_lane,
        )
        return list(
            candidate.select()
            .where(
                candidate.state.in_(TaskState.LANE_HEAD_STATES),
                ~fn.EXISTS(earlier_query),
            )
            .order_by(
                candidate.priority.desc(),
                candidate.accepted_at.asc(),
                candidate.id.asc(),
            )
        )

    @staticmethod
    def mark_in_flight(
        task_id: int,
        *,
        lease_owner: str,
        submitted_at: datetime.datetime,
    ) -> bool:
        updated = (
            OutboundTask.update(
                state=TaskState.IN_FLIGHT,
                submitted_at=submitted_at,
            )
            .where(
                OutboundTask.id == task_id,
                OutboundTask.state == TaskState.LEASED,
                OutboundTask.lease_owner == lease_owner,
            )
            .execute()
        )
        return bool(updated)

    @staticmethod
    def lease(
        task_id: int,
        *,
        lease_owner: str,
        now: datetime.datetime,
        lease_duration: datetime.timedelta,
    ) -> Optional[OutboundTask]:
        lease_until = now + lease_duration
        updated = (
            OutboundTask.update(
                state=TaskState.LEASED,
                lease_owner=lease_owner,
                lease_until=lease_until,
                lease_heartbeat_at=now,
                attempt_count=OutboundTask.attempt_count + 1,
            )
            .where(
                OutboundTask.id == task_id,
                OutboundTask.state == TaskState.QUEUED,
                (
                    OutboundTask.available_at.is_null(True)
                    | (OutboundTask.available_at <= now)
                ),
            )
            .execute()
        )
        if not updated:
            return None
        return OutboundTask.get_by_id(task_id)

    @staticmethod
    def requeue(
        task_id: int,
        *,
        available_at: Optional[datetime.datetime],
        error_class: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> bool:
        updated = (
            OutboundTask.update(
                state=TaskState.QUEUED,
                available_at=available_at,
                lease_owner=None,
                lease_until=None,
                lease_heartbeat_at=None,
                submitted_at=None,
                error_class=error_class,
                last_error=last_error,
            )
            .where(
                OutboundTask.id == task_id,
                OutboundTask.state.in_((TaskState.LEASED, TaskState.IN_FLIGHT)),
            )
            .execute()
        )
        return bool(updated)

    def migrate_chat_target(self, old_chat_id: int, new_chat_id: int) -> int:
        migratable_states = (
            TaskState.WAITING_DEPENDENCY,
            TaskState.QUEUED,
            TaskState.LEASED,
            TaskState.IN_FLIGHT,
        )

        def matches_old_chat(value: object) -> bool:
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                return False
            try:
                return int(value) == old_chat_id
            except ValueError:
                return False

        updated = 0
        with OutboundTask._meta.database.atomic():
            tasks = list(
                OutboundTask.select()
                .where(
                    OutboundTask.target_chat_id == old_chat_id,
                    OutboundTask.state.in_(migratable_states),
                )
                .order_by(OutboundTask.id.asc())
            )
            for task in tasks:
                document = json.loads(task.payload)
                args_document = document.get("args")
                if (
                    isinstance(args_document, dict)
                    and args_document.get(self.codec.TYPE_KEY) == "tuple"
                    and isinstance(args_document.get("items"), list)
                    and args_document["items"]
                    and matches_old_chat(args_document["items"][0])
                ):
                    args_document["items"][0] = new_chat_id

                kwargs_document = document.get("kwargs")
                if (
                    isinstance(kwargs_document, dict)
                    and matches_old_chat(kwargs_document.get("chat_id"))
                ):
                    kwargs_document["chat_id"] = new_chat_id

                payload = json.dumps(document, separators=(",", ":"), sort_keys=True)
                updated += int(
                    OutboundTask.update(
                        target_chat_id=new_chat_id,
                        payload=payload,
                    )
                    .where(
                        OutboundTask.id == task.id,
                        OutboundTask.state.in_(migratable_states),
                    )
                    .execute()
                )
        return updated

    @staticmethod
    def heartbeat(
        task_ids: Sequence[int],
        *,
        lease_owner: str,
        now: datetime.datetime,
        lease_duration: datetime.timedelta,
    ) -> int:
        if not task_ids:
            return 0
        return int(
            OutboundTask.update(
                lease_heartbeat_at=now,
                lease_until=now + lease_duration,
            )
            .where(
                OutboundTask.id.in_(task_ids),
                OutboundTask.state == TaskState.IN_FLIGHT,
                OutboundTask.lease_owner == lease_owner,
            )
            .execute()
        )

    @staticmethod
    def record_sent(
        task_id: int,
        result_payload: Mapping[str, object],
    ) -> OutboundTask:
        payload_json = json.dumps(result_payload, separators=(",", ":"), sort_keys=True)
        (
            OutboundTask.update(
                state=TaskState.SENT_PENDING_LOG,
                result_payload=payload_json,
                error_class=None,
                last_error=None,
            )
            .where(OutboundTask.id == task_id)
            .execute()
        )
        return OutboundTask.get_by_id(task_id)

    @staticmethod
    def has_error_handler(task_id: int, error_class: str) -> bool:
        return OutboundTask.select().where(
            OutboundTask.depends_on_task_id == task_id,
            OutboundTask.state == TaskState.WAITING_DEPENDENCY,
            OutboundTask.run_condition == f"{RunCondition.PREDECESSOR_ERROR_PREFIX}{error_class}",
        ).exists()

    @staticmethod
    def sent_pending_tasks(task_ids: Optional[Sequence[int]] = None) -> list[OutboundTask]:
        query = OutboundTask.select().where(OutboundTask.state == TaskState.SENT_PENDING_LOG)
        if task_ids is not None:
            if not task_ids:
                return []
            query = query.where(OutboundTask.id.in_(task_ids))
        return list(query.order_by(OutboundTask.id.asc()))

    def complete_success(self, task_id: int, now: datetime.datetime) -> Optional[WorkflowOutcome]:
        with OutboundTask._meta.database.atomic():
            task = OutboundTask.get_by_id(task_id)
            (
                OutboundTask.update(
                    state=TaskState.COMPLETED,
                    lease_owner=None,
                    lease_until=None,
                    lease_heartbeat_at=None,
                )
                .where(OutboundTask.id == task_id)
                .execute()
            )
            cleanup_tasks = self._advance_dependencies(task, succeeded=True, error_class=None)
            outcome = self._finish_workflow_if_terminal(task.workflow_id, now)
        self._cleanup_task_media(task)
        for cleanup_task in cleanup_tasks:
            self._cleanup_task_media(cleanup_task)
        return outcome

    def complete_expected_error(
        self,
        task_id: int,
        *,
        error_class: str,
        last_error: str,
        now: datetime.datetime,
    ) -> Optional[WorkflowOutcome]:
        with OutboundTask._meta.database.atomic():
            task = OutboundTask.get_by_id(task_id)
            result_payload = json.dumps(
                {"ok": False, "error_class": error_class, "error": last_error},
                separators=(",", ":"),
                sort_keys=True,
            )
            (
                OutboundTask.update(
                    state=TaskState.COMPLETED,
                    result_payload=result_payload,
                    error_class=error_class,
                    last_error=last_error,
                    lease_owner=None,
                    lease_until=None,
                    lease_heartbeat_at=None,
                )
                .where(OutboundTask.id == task_id)
                .execute()
            )
            cleanup_tasks = self._advance_dependencies(
                task,
                succeeded=False,
                error_class=error_class,
            )
            outcome = self._finish_workflow_if_terminal(task.workflow_id, now)
        self._cleanup_task_media(task)
        for cleanup_task in cleanup_tasks:
            self._cleanup_task_media(cleanup_task)
        return outcome

    def mark_dead(
        self,
        task_id: int,
        *,
        error_class: str,
        last_error: str,
        now: datetime.datetime,
    ) -> Optional[WorkflowOutcome]:
        with OutboundTask._meta.database.atomic():
            task = OutboundTask.get_by_id(task_id)
            (
                OutboundTask.update(
                    state=TaskState.DEAD,
                    error_class=error_class,
                    last_error=last_error,
                    lease_owner=None,
                    lease_until=None,
                    lease_heartbeat_at=None,
                )
                .where(OutboundTask.id == task_id)
                .execute()
            )
            cleanup_tasks = self._skip_descendants(task.id)
            outcome = self._finish_workflow_if_terminal(task.workflow_id, now)
        self._cleanup_task_media(task)
        for cleanup_task in cleanup_tasks:
            self._cleanup_task_media(cleanup_task)
        return outcome

    def _advance_dependencies(
        self,
        predecessor: OutboundTask,
        *,
        succeeded: bool,
        error_class: Optional[str],
    ) -> list[OutboundTask]:
        cleanup_tasks: list[OutboundTask] = []
        dependents = list(
            OutboundTask.select()
            .where(
                OutboundTask.depends_on_task_id == predecessor.id,
                OutboundTask.state == TaskState.WAITING_DEPENDENCY,
            )
            .order_by(OutboundTask.step_index.asc(), OutboundTask.id.asc())
        )
        matching_result_task_id: Optional[int] = None
        for dependent in dependents:
            matches = dependent.run_condition == RunCondition.ALWAYS
            if dependent.run_condition == RunCondition.PREDECESSOR_SUCCESS:
                matches = succeeded
            elif dependent.run_condition.startswith(RunCondition.PREDECESSOR_ERROR_PREFIX):
                expected = dependent.run_condition[len(RunCondition.PREDECESSOR_ERROR_PREFIX):]
                matches = not succeeded and error_class == expected
            if matches:
                (
                    OutboundTask.update(state=TaskState.QUEUED, available_at=None)
                    .where(OutboundTask.id == dependent.id)
                    .execute()
                )
                if not succeeded and matching_result_task_id is None:
                    matching_result_task_id = dependent.id
            else:
                OutboundTask.update(state=TaskState.SKIPPED).where(OutboundTask.id == dependent.id).execute()
                cleanup_tasks.append(dependent)
                cleanup_tasks.extend(self._skip_descendants(dependent.id))

        workflow = OutboundWorkflow.get_by_id(predecessor.workflow_id)
        if workflow.result_task_id == predecessor.id and matching_result_task_id is not None:
            (
                OutboundWorkflow.update(result_task_id=matching_result_task_id)
                .where(OutboundWorkflow.id == workflow.id)
                .execute()
            )
        return cleanup_tasks

    def _skip_descendants(self, predecessor_id: int) -> list[OutboundTask]:
        cleanup_tasks: list[OutboundTask] = []
        dependents = list(
            OutboundTask.select()
            .where(
                OutboundTask.depends_on_task_id == predecessor_id,
                OutboundTask.state == TaskState.WAITING_DEPENDENCY,
            )
        )
        for dependent in dependents:
            OutboundTask.update(state=TaskState.SKIPPED).where(OutboundTask.id == dependent.id).execute()
            cleanup_tasks.append(dependent)
            cleanup_tasks.extend(self._skip_descendants(dependent.id))
        return cleanup_tasks

    def _cleanup_task_media(self, task: OutboundTask) -> None:
        if not task.media_ref:
            return
        media_refs = json.loads(task.media_ref)
        if isinstance(media_refs, list) and all(isinstance(item, str) for item in media_refs):
            self.codec.cleanup_media_refs(media_refs)

    def cleanup_orphaned_spool_files(self) -> int:
        referenced: set[str] = set()
        for task in OutboundTask.select(OutboundTask.media_ref).where(
            OutboundTask.media_ref.is_null(False),
            OutboundTask.state.in_(TaskState.ACTIVE),
        ):
            media_ref = task.media_ref
            if media_ref is None:
                continue
            try:
                media_refs = json.loads(media_ref)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(media_refs, list):
                referenced.update(item for item in media_refs if isinstance(item, str))

        removed = 0
        for path in self.codec.spool_dir.iterdir():
            if not path.is_file() or path.name in referenced:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                logging.getLogger(__name__).warning(
                    "Failed to remove orphaned outbound spool file %s: %s",
                    path,
                    error,
                )
            else:
                removed += 1
        return removed

    @staticmethod
    def _finish_workflow_if_terminal(
        workflow_id: int,
        now: datetime.datetime,
    ) -> Optional[WorkflowOutcome]:
        workflow = OutboundWorkflow.get_by_id(workflow_id)
        if workflow.state != "active":
            return None
        tasks = list(OutboundTask.select().where(OutboundTask.workflow_id == workflow_id))
        if any(task.state in TaskState.ACTIVE for task in tasks):
            return None
        dead_task = next((task for task in tasks if task.state == TaskState.DEAD), None)
        workflow_state = "dead" if dead_task is not None else "completed"
        error_class = dead_task.error_class if dead_task is not None else None
        (
            OutboundWorkflow.update(
                state=workflow_state,
                error_class=error_class,
                completed_at=now,
            )
            .where(OutboundWorkflow.id == workflow_id, OutboundWorkflow.state == "active")
            .execute()
        )
        result_task = (
            OutboundTask.get_or_none(OutboundTask.id == workflow.result_task_id)
            if workflow.result_task_id is not None else None
        )
        result_payload = None
        if result_task is not None and result_task.result_payload:
            result_payload = json.loads(result_task.result_payload)
        return WorkflowOutcome(
            workflow_id=workflow_id,
            state=workflow_state,
            result_task_id=workflow.result_task_id,
            result_payload=result_payload,
            error_class=error_class,
        )

    @staticmethod
    def recover(
        now: datetime.datetime,
        *,
        local_in_flight_task_ids: Sequence[int] = (),
    ) -> RecoveryPlan:
        expired_query = OutboundTask.select(OutboundTask.id).where(
            OutboundTask.state.in_((TaskState.LEASED, TaskState.IN_FLIGHT)),
            (
                OutboundTask.lease_until.is_null(True)
                | (OutboundTask.lease_until <= now)
            ),
        )
        if local_in_flight_task_ids:
            expired_query = expired_query.where(
                OutboundTask.id.not_in(tuple(local_in_flight_task_ids))
            )
        expired_rows = list(expired_query.order_by(OutboundTask.id.asc()))
        expired_ids = tuple(row.id for row in expired_rows)
        if expired_ids:
            (
                OutboundTask.update(
                    state=TaskState.QUEUED,
                    available_at=now,
                    lease_owner=None,
                    lease_until=None,
                    lease_heartbeat_at=None,
                    submitted_at=None,
                    error_class="ambiguous_recovery",
                    last_error="Process exited after Telegram submission state became ambiguous.",
                )
                .where(OutboundTask.id.in_(expired_ids))
                .execute()
            )
        sent_ids = tuple(
            row.id
            for row in OutboundTask.select(OutboundTask.id)
            .where(OutboundTask.state == TaskState.SENT_PENDING_LOG)
            .order_by(OutboundTask.id.asc())
        )
        return RecoveryPlan(
            requeued_ambiguous_ids=expired_ids,
            sent_pending_log_ids=sent_ids,
        )

class OutboundScheduler:
    """Fill bounded worker capacity from stateless durable source lane heads."""

    LEASE_DURATION = datetime.timedelta(minutes=5)

    def __init__(
        self,
        repository: OutboundRepository,
        adapter: OutboundSchedulerAdapter,
        *,
        executor: Executor,
        worker_count: int,
        lease_owner: str,
        lease_duration: datetime.timedelta = LEASE_DURATION,
    ):
        if worker_count <= 0:
            raise ValueError("worker_count must be positive.")
        self.repository = repository
        self.adapter = adapter
        self.executor = executor
        self.lease_owner = lease_owner
        self.lease_duration = lease_duration
        self.worker_permits = threading.BoundedSemaphore(worker_count)
        self.wake_event = threading.Event()
        self._in_flight: dict[int, InFlightTask] = {}
        self._in_flight_lock = threading.Lock()
        self.logger = logging.getLogger(__name__)

    def dispatch_ready(self, now: datetime.datetime) -> int:
        submitted = 0
        while True:
            local_in_flight_ids = set(self.in_flight_task_ids())
            heads = [
                task
                for task in self.repository.list_lane_heads(now)
                if task.state == TaskState.QUEUED
                and (task.available_at is None or task.available_at <= now)
                and task.id not in local_in_flight_ids
            ]
            if not heads:
                break
            if any(task.priority for task in heads):
                heads = [task for task in heads if task.priority]

            submitted_this_round = 0
            for head in heads:
                if not self.worker_permits.acquire(blocking=False):
                    return submitted
                permit_transferred = False
                task: Optional[OutboundTask] = None
                selection: Optional[SenderSelection] = None
                future: Optional[Future] = None
                task_resolved = False
                reservation_released = False
                try:
                    task = self.repository.lease(
                        head.id,
                        lease_owner=self.lease_owner,
                        now=now,
                        lease_duration=self.lease_duration,
                    )
                    if task is None:
                        continue

                    selection_result = self.adapter.select_sender(task, now)
                    selection = selection_result.selection
                    if selection is None:
                        if selection_result.terminal_error_class is not None:
                            outcome = self.repository.mark_dead(
                                task.id,
                                error_class=selection_result.terminal_error_class,
                                last_error=selection_result.reason,
                                now=now,
                            )
                            if outcome is not None:
                                self.adapter.workflow_finished(outcome)
                        else:
                            self.repository.requeue(
                                task.id,
                                available_at=selection_result.retry_at,
                                error_class=selection_result.reason,
                            )
                        task_resolved = True
                        continue

                    try:
                        future = self.executor.submit(self.adapter.execute_task, task, selection)
                    except Exception as error:
                        try:
                            self.adapter.release_reservation(selection)
                            reservation_released = True
                        finally:
                            self.repository.requeue(
                                task.id,
                                available_at=now,
                                error_class="executor_submit",
                                last_error=str(error),
                            )
                            task_resolved = True
                        continue

                    try:
                        marked_in_flight = self.repository.mark_in_flight(
                            task.id,
                            lease_owner=self.lease_owner,
                            submitted_at=now,
                        )
                    except Exception:
                        marked_in_flight = False
                        self.logger.exception(
                            "Outbound task %s was submitted but its in-flight state could not be persisted.",
                            task.id,
                        )
                    if not marked_in_flight:
                        self.logger.error(
                            "Outbound task %s was submitted without its matching lease state; "
                            "the local future remains authoritative until completion.",
                            task.id,
                        )

                    in_flight = InFlightTask(
                        future=future,
                        task_id=task.id,
                        selection=selection,
                    )
                    with self._in_flight_lock:
                        self._in_flight[task.id] = in_flight
                    future.add_done_callback(self._future_done)
                    permit_transferred = True
                    task_dispatched = getattr(self.adapter, "task_dispatched", None)
                    if callable(task_dispatched):
                        try:
                            task_dispatched(task, selection, now)
                        except Exception:
                            self.logger.exception("Failed to record dispatch for outbound task %s.", task.id)
                    submitted += 1
                    submitted_this_round += 1
                except Exception as error:
                    if future is None and task is not None and not task_resolved:
                        if selection is not None and not reservation_released:
                            try:
                                self.adapter.release_reservation(selection)
                            except Exception:
                                self.logger.exception(
                                    "Failed to release reservation after dispatch error for task %s.",
                                    task.id,
                                )
                        self.repository.requeue(
                            task.id,
                            available_at=now,
                            error_class="dispatch_error",
                            last_error=str(error),
                        )
                    raise
                finally:
                    if not permit_transferred:
                        self.worker_permits.release()

            if submitted_this_round == 0:
                break
        return submitted

    def _future_done(self, _future: Future) -> None:
        self.worker_permits.release()
        self.wake_event.set()

    def harvest_completed(self, now: datetime.datetime) -> int:
        with self._in_flight_lock:
            completed = [item for item in self._in_flight.values() if item.future.done()]
            for item in completed:
                self._in_flight.pop(item.task_id, None)

        for in_flight in completed:
            task = OutboundTask.get_by_id(in_flight.task_id)
            try:
                result = in_flight.future.result()
            except Exception as error:
                disposition = self.adapter.classify_error(task, error, in_flight.selection, now)
                if disposition.kind == FailureDisposition.RETRY:
                    self.adapter.release_reservation(in_flight.selection)
                    self.repository.requeue(
                        task.id,
                        available_at=disposition.retry_at,
                        error_class=disposition.error_class,
                        last_error=str(error),
                    )
                    outcome = None
                elif disposition.kind == FailureDisposition.EXPECTED:
                    self.adapter.release_reservation(in_flight.selection)
                    outcome = self.repository.complete_expected_error(
                        task.id,
                        error_class=disposition.error_class,
                        last_error=str(error),
                        now=now,
                    )
                else:
                    self.adapter.release_reservation(in_flight.selection)
                    outcome = self.repository.mark_dead(
                        task.id,
                        error_class=disposition.error_class,
                        last_error=str(error),
                        now=now,
                    )
            else:
                result_payload = dict(self.adapter.serialize_result(task, result, in_flight.selection))
                sent_task = self.repository.record_sent(task.id, result_payload)
                try:
                    self.adapter.reconcile_sent_task(sent_task, result_payload, in_flight.selection)
                except Exception:
                    self.logger.exception(
                        "Outbound task %s was sent but its message log reconciliation failed.",
                        task.id,
                    )
                    outcome = None
                else:
                    outcome = self.repository.complete_success(task.id, now)
            if outcome is not None:
                self.adapter.workflow_finished(outcome)
        return len(completed)

    def heartbeat(self, now: datetime.datetime) -> int:
        with self._in_flight_lock:
            task_ids = tuple(self._in_flight)
        return self.repository.heartbeat(
            task_ids,
            lease_owner=self.lease_owner,
            now=now,
            lease_duration=self.lease_duration,
        )

    def in_flight_snapshot(self) -> tuple[InFlightTask, ...]:
        with self._in_flight_lock:
            return tuple(self._in_flight.values())

    def in_flight_task_ids(self) -> tuple[int, ...]:
        with self._in_flight_lock:
            return tuple(self._in_flight)

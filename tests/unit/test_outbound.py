import datetime
import io
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from peewee import SqliteDatabase
import pytest
import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile

from efb_telegram_master.bot_manager import TelegramBotManager
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.db import (
    DatabaseManager,
    HistoryMigrationEntry,
    MsgLog,
    OutboundTask,
    OutboundWorkflow,
)
from efb_telegram_master.outbound import (
    OutboundPayloadCodec,
    OutboundRepository,
    OutboundScheduler,
    OutboundTaskSpec,
    SenderSelection,
    SenderSelectionResult,
    FailureDisposition,
    RunCondition,
    TaskState,
    WorkflowOutcome,
)
from efb_telegram_master.rate_limiter import SlidingWindowRateLimiter


def _repository(tmp_path):
    codec = OutboundPayloadCodec(tmp_path / "spool")
    return OutboundRepository(codec), codec


def _spec(source_key="module chat", *, priority=False, state=TaskState.QUEUED,
          depends_on_task_id=None, run_condition="always", text="hello"):
    return OutboundTaskSpec(
        source_key=source_key,
        slave_id=source_key,
        priority=priority,
        target_chat_id=100,
        message_thread_id=None,
        operation="send_message",
        args=(),
        kwargs={"chat_id": 100, "text": text},
        state=state,
        depends_on_task_id=depends_on_task_id,
        run_condition=run_condition,
    )


def test_outbound_schema_contains_recovery_and_ordering_fields():
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask, MsgLog]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        task_columns = {column.name for column in test_db.get_columns("outboundtask")}
        msglog_columns = {column.name for column in test_db.get_columns("msglog")}

    assert {
        "source_key", "slave_id", "priority", "target_chat_id", "operation",
        "payload", "media_ref", "workflow_id", "step_index",
        "depends_on_task_id", "run_condition", "result_payload",
        "required_sender_bot_id", "state", "available_at", "lease_owner",
        "lease_until", "lease_heartbeat_at", "submitted_at", "attempt_count",
        "accepted_at",
    }.issubset(task_columns)
    assert "outbound_task_id" in msglog_columns


def test_payload_codec_spools_files_and_round_trips_telegram_objects(tmp_path):
    codec = OutboundPayloadCodec(tmp_path / "spool")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open", url="https://example.com")]])
    source = InputFile(io.BytesIO(b"durable bytes"), filename="payload.bin")

    encoded = codec.encode_command(
        "send_document",
        (),
        {"chat_id": 100, "document": source, "reply_markup": markup},
    )
    operation, args, kwargs = codec.decode_command(encoded.payload)

    assert operation == "send_document"
    assert args == ()
    assert kwargs["document"].input_file_content == b"durable bytes"
    assert kwargs["document"].filename == "payload.bin"
    assert kwargs["reply_markup"] == markup
    assert len(encoded.media_refs) == 1
    assert (tmp_path / "spool" / encoded.media_refs[0]).read_bytes() == b"durable bytes"


def test_payload_codec_claims_managed_local_file_uri_before_source_cleanup(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    source_path = tmp_path / "shared" / "payload.bin"
    source_path.parent.mkdir()
    source_path.write_bytes(b"local bot api bytes")

    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, codec = _repository(tmp_path)
        created = repository.create_workflow([
            OutboundTaskSpec(
                **{
                    **_spec().__dict__,
                    "operation": "api_send_document",
                    "kwargs": {
                        "chat_id": 100,
                        "document": source_path.as_uri(),
                        "caption": "x" * 10_000,
                    },
                    "owned_local_paths": (str(source_path),),
                }
            )
        ])
        source_path.unlink()
        _operation, _args, kwargs = codec.decode_command(created.tasks[0].payload)

    assert kwargs["document"].read() == b"local bot api bytes"
    assert kwargs["caption"] == "x" * 10_000
    assert len(json.loads(created.tasks[0].media_ref)) == 1


def test_repository_assigns_database_order_and_selects_priority_lane_head(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        normal = repository.create_workflow([_spec(text="normal")]).tasks[0]
        blocking = repository.create_workflow([_spec(priority=True, text="blocking")]).tasks[0]

        head = repository.list_lane_heads(datetime.datetime.now())[0]

    assert normal.accepted_at is not None
    assert blocking.accepted_at is not None
    assert normal.id < blocking.id
    assert head.id == blocking.id


def test_lane_head_query_materializes_only_one_row_per_source(tmp_path, monkeypatch):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        for source in ("source-a", "source-b"):
            for index in range(10):
                repository.create_workflow([_spec(source_key=source, text=str(index))])

        materialized = 0
        original_init = OutboundTask.__init__

        def counting_init(self, *args, **kwargs):
            nonlocal materialized
            materialized += 1
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(OutboundTask, "__init__", counting_init)
        heads = repository.list_lane_heads(datetime.datetime.now())

    assert len(heads) == 2
    assert materialized == 2


def test_waiting_dependency_blocks_later_lane_rows(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        predecessor = repository.create_workflow([_spec(text="first")]).tasks[0]
        repository.create_workflow([
            _spec(
                state=TaskState.WAITING_DEPENDENCY,
                depends_on_task_id=predecessor.id,
                run_condition="predecessor_success",
                text="dependent",
            )
        ])
        repository.create_workflow([_spec(text="later")])

        OutboundTask.update(state=TaskState.IN_FLIGHT).where(
            OutboundTask.id == predecessor.id
        ).execute()
        heads = repository.list_lane_heads(datetime.datetime.now())

    assert len(heads) == 1
    assert heads[0].state == TaskState.WAITING_DEPENDENCY


def test_unexpired_lease_blocks_later_rows_until_recovery(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        leased = repository.create_workflow([_spec(text="leased")]).tasks[0]
        later = repository.create_workflow([_spec(text="later")]).tasks[0]
        now = datetime.datetime.now()
        OutboundTask.update(
            state=TaskState.LEASED,
            lease_owner="other-process",
            lease_until=now + datetime.timedelta(minutes=1),
        ).where(OutboundTask.id == leased.id).execute()

        heads = repository.list_lane_heads(now)
        recovery = repository.recover(now)

    assert [head.id for head in heads] == [leased.id]
    assert later.id not in [head.id for head in heads]
    assert recovery.requeued_ambiguous_ids == ()


def test_recovery_requeues_expired_in_flight_and_reconciles_sent_rows(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        expired = repository.create_workflow([_spec(text="expired")]).tasks[0]
        sent = repository.create_workflow([_spec(source_key="module other", text="sent")]).tasks[0]
        past = datetime.datetime.now() - datetime.timedelta(seconds=1)
        OutboundTask.update(
            state=TaskState.IN_FLIGHT,
            lease_owner="dead-process",
            lease_until=past,
        ).where(OutboundTask.id == expired.id).execute()
        OutboundTask.update(
            state=TaskState.SENT_PENDING_LOG,
            result_payload=json.dumps({"message_id": 1, "chat_id": 100}),
        ).where(OutboundTask.id == sent.id).execute()

        recovery = repository.recover(datetime.datetime.now())
        recovered_state = OutboundTask.get_by_id(expired.id).state

    assert recovery.requeued_ambiguous_ids == (expired.id,)
    assert recovery.sent_pending_log_ids == (sent.id,)
    assert recovered_state == TaskState.QUEUED


def test_chat_migration_rewrites_all_active_workflow_targets(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, codec = _repository(tmp_path)
        positional = repository.create_workflow([
            OutboundTaskSpec(
                **{
                    **_spec(source_key="module a").__dict__,
                    "args": (100, "positional"),
                    "kwargs": {},
                }
            )
        ]).tasks[0]
        keyword = repository.create_workflow([
            OutboundTaskSpec(
                **{
                    **_spec(source_key="module b").__dict__,
                    "args": (),
                    "kwargs": {"chat_id": 100, "text": "keyword"},
                }
            )
        ]).tasks[0]
        sent = repository.create_workflow([
            OutboundTaskSpec(
                **{
                    **_spec(source_key="module c").__dict__,
                    "args": (100, "sent"),
                    "kwargs": {},
                }
            )
        ]).tasks[0]
        OutboundTask.update(state=TaskState.SENT_PENDING_LOG).where(
            OutboundTask.id == sent.id
        ).execute()

        assert repository.migrate_chat_target(100, -100200) == 2
        positional_row = OutboundTask.get_by_id(positional.id)
        keyword_row = OutboundTask.get_by_id(keyword.id)
        sent_row = OutboundTask.get_by_id(sent.id)
        _operation, positional_args, _kwargs = codec.decode_command(positional_row.payload)
        _operation, _args, keyword_kwargs = codec.decode_command(keyword_row.payload)
        _operation, sent_args, _kwargs = codec.decode_command(sent_row.payload)

    assert positional_row.target_chat_id == -100200
    assert positional_args[0] == -100200
    assert keyword_row.target_chat_id == -100200
    assert keyword_kwargs["chat_id"] == -100200
    assert sent_row.target_chat_id == 100
    assert sent_args[0] == 100


def test_chat_migration_payload_rewrite_rolls_back_on_malformed_row(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        first = repository.create_workflow([_spec(source_key="module a")]).tasks[0]
        malformed = repository.create_workflow([_spec(source_key="module b")]).tasks[0]
        OutboundTask.update(payload="{").where(OutboundTask.id == malformed.id).execute()

        with pytest.raises(json.JSONDecodeError):
            repository.migrate_chat_target(100, -100200)
        first_row = OutboundTask.get_by_id(first.id)

    assert first_row.target_chat_id == 100


class RecordingExecutor:
    def __init__(self):
        self.submissions = []

    def submit(self, function, *args):
        future = Future()
        self.submissions.append((function, args, future))
        return future


class FailingExecutor:
    def submit(self, _function, *_args):
        raise RuntimeError("executor rejected submission")


class SchedulerAdapter:
    def __init__(self):
        self.limiter = SlidingWindowRateLimiter(
            global_limit=100,
            chat_limit=100,
            safety_margin=0,
            owner_id="test-bot",
        )
        self.released = []
        self.finished = []

    def select_sender(self, task, _now):
        outcome = self.limiter.reserve_slot(task.target_chat_id)
        return SenderSelectionResult(
            selection=SenderSelection(
                sender=object(),
                sender_bot_id="test-bot",
                reservation=outcome.reservation,
            )
        )

    def execute_task(self, task, _selection):
        return {"task_id": task.id}

    def release_reservation(self, selection):
        self.limiter.release_slot(selection.reservation)
        self.released.append(selection.reservation)

    def serialize_result(self, task, result, selection):
        return {"task_id": task.id, "result": result, "sender_bot_id": selection.sender_bot_id}

    def classify_error(self, _task, error, _selection, _now):
        raise error

    def reconcile_sent_task(self, _task, _result_payload, _selection):
        return None

    def workflow_finished(self, workflow_outcome):
        self.finished.append(workflow_outcome)


def _scheduler_fixture(tmp_path, specs, worker_count=2):
    repository, codec = _repository(tmp_path)
    created = [repository.create_workflow([spec]) for spec in specs]
    executor = RecordingExecutor()
    adapter = SchedulerAdapter()
    scheduler = OutboundScheduler(
        repository,
        adapter,
        executor=executor,
        worker_count=worker_count,
        lease_owner="test-process",
    )
    return scheduler, executor, adapter, created, codec


def test_scheduler_submits_same_source_in_database_order_before_completion(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        scheduler, executor, _adapter, created, _codec = _scheduler_fixture(
            tmp_path,
            [_spec(text="first"), _spec(text="second")],
        )

        scheduler.dispatch_ready(datetime.datetime.now())

        submitted_ids = [args[0].id for _function, args, _future in executor.submissions]
        states = [OutboundTask.get_by_id(task.id).state for workflow in created for task in workflow.tasks]

    assert submitted_ids == [created[0].tasks[0].id, created[1].tasks[0].id]
    assert states == [TaskState.IN_FLIGHT, TaskState.IN_FLIGHT]


def test_scheduler_submits_independent_sources_sharing_one_telegram_chat(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        scheduler, executor, _adapter, _created, _codec = _scheduler_fixture(
            tmp_path,
            [_spec(source_key="module a"), _spec(source_key="module b")],
        )

        scheduler.dispatch_ready(datetime.datetime.now())

    assert [args[0].source_key for _function, args, _future in executor.submissions] == [
        "module a", "module b",
    ]


def test_same_priority_ready_source_remains_eligible_beside_busy_source(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        scheduler, executor, _adapter, created, _codec = _scheduler_fixture(
            tmp_path,
            [
                _spec(source_key="module a", text="a-1"),
                _spec(source_key="module a", text="a-2"),
                _spec(source_key="module a", text="a-3"),
                _spec(source_key="module b", text="b-1"),
            ],
            worker_count=3,
        )

        now = datetime.datetime.now()
        scheduler.dispatch_ready(now)
        initial_ids = [args[0].id for _function, args, _future in executor.submissions]
        executor.submissions[0][2].set_result({"message_id": 1})
        scheduler.harvest_completed(now)
        scheduler.dispatch_ready(now)

    assert initial_ids == [
        created[0].tasks[0].id,
        created[3].tasks[0].id,
        created[1].tasks[0].id,
    ]
    assert [args[0].id for _function, args, _future in executor.submissions] == [
        created[0].tasks[0].id,
        created[3].tasks[0].id,
        created[1].tasks[0].id,
        created[2].tasks[0].id,
    ]


def test_blocked_lane_head_submits_before_later_row_when_capacity_returns(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        scheduler, executor, adapter, created, _codec = _scheduler_fixture(
            tmp_path,
            [_spec(text="first"), _spec(text="second")],
        )
        first_id = created[0].tasks[0].id
        select_sender = adapter.select_sender
        sender_available = False

        def controlled_selection(task, now):
            if task.id == first_id and not sender_available:
                return SenderSelectionResult(
                    retry_at=now + datetime.timedelta(seconds=1),
                    reason="all_senders_rate_limited",
                )
            return select_sender(task, now)

        adapter.select_sender = controlled_selection
        now = datetime.datetime.now()
        scheduler.dispatch_ready(now)
        blocked_states = [
            OutboundTask.get_by_id(workflow.tasks[0].id).state
            for workflow in created
        ]

        sender_available = True
        scheduler.dispatch_ready(now + datetime.timedelta(seconds=2))

    assert blocked_states == [TaskState.QUEUED, TaskState.QUEUED]
    assert [args[0].id for _function, args, _future in executor.submissions] == [
        created[0].tasks[0].id,
        created[1].tasks[0].id,
    ]


def test_blocking_source_submits_while_other_source_normal_task_is_in_flight(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        scheduler, executor, _adapter, created, _codec = _scheduler_fixture(
            tmp_path,
            [_spec(source_key="module a", text="normal")],
            worker_count=2,
        )
        scheduler.dispatch_ready(datetime.datetime.now())
        blocking = scheduler.repository.create_workflow([
            _spec(source_key="module b", priority=True, text="blocking")
        ])

        scheduler.dispatch_ready(datetime.datetime.now())

    assert executor.submissions[0][1][0].id == created[0].tasks[0].id
    assert executor.submissions[0][2].done() is False
    assert executor.submissions[1][1][0].id == blocking.tasks[0].id


def test_blocking_and_normal_lanes_each_keep_fifo_submission_order(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        scheduler, executor, _adapter, created, _codec = _scheduler_fixture(
            tmp_path,
            [
                _spec(text="normal-1"),
                _spec(priority=True, text="blocking-1"),
                _spec(text="normal-2"),
                _spec(priority=True, text="blocking-2"),
            ],
            worker_count=4,
        )

        scheduler.dispatch_ready(datetime.datetime.now())

    assert [args[0].id for _function, args, _future in executor.submissions] == [
        created[1].tasks[0].id,
        created[3].tasks[0].id,
        created[0].tasks[0].id,
        created[2].tasks[0].id,
    ]


def test_scheduler_does_not_lease_or_submit_beyond_worker_permits(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        scheduler, executor, _adapter, created, _codec = _scheduler_fixture(
            tmp_path,
            [_spec(source_key="module a"), _spec(source_key="module b"), _spec(source_key="module c")],
            worker_count=2,
        )

        scheduler.dispatch_ready(datetime.datetime.now())
        third_state = OutboundTask.get_by_id(created[2].tasks[0].id).state

    assert len(executor.submissions) == 2
    assert third_state == TaskState.QUEUED


def test_scheduler_releases_permit_and_requeues_when_sender_selection_raises(tmp_path):
    class RaisingSelectionAdapter(SchedulerAdapter):
        def __init__(self):
            super().__init__()
            self.raise_once = True

        def select_sender(self, task, now):
            if self.raise_once:
                self.raise_once = False
                raise RuntimeError("selection failed")
            return super().select_sender(task, now)

    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        created = repository.create_workflow([_spec()])
        executor = RecordingExecutor()
        adapter = RaisingSelectionAdapter()
        scheduler = OutboundScheduler(
            repository,
            adapter,
            executor=executor,
            worker_count=1,
            lease_owner="test-process",
        )
        now = datetime.datetime.now()

        with pytest.raises(RuntimeError, match="selection failed"):
            scheduler.dispatch_ready(now)
        failed_state = OutboundTask.get_by_id(created.tasks[0].id).state
        scheduler.dispatch_ready(now)

    assert failed_state == TaskState.QUEUED
    assert len(executor.submissions) == 1


def test_executor_submit_failure_releases_exact_reservation_and_permit(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        created = repository.create_workflow([_spec()])
        adapter = SchedulerAdapter()
        scheduler = OutboundScheduler(
            repository,
            adapter,
            executor=FailingExecutor(),
            worker_count=1,
            lease_owner="test-process",
        )
        now = datetime.datetime.now()

        assert scheduler.dispatch_ready(now) == 0
        failed_state = OutboundTask.get_by_id(created.tasks[0].id).state
        reservation_count = adapter.limiter.get_reserved_slot_count()
        executor = RecordingExecutor()
        scheduler.executor = executor
        scheduler.dispatch_ready(now)

    assert failed_state == TaskState.QUEUED
    assert reservation_count == 0
    assert len(executor.submissions) == 1


def test_reverse_completion_does_not_change_local_submission_order(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        scheduler, executor, adapter, created, _codec = _scheduler_fixture(
            tmp_path,
            [_spec(text="first"), _spec(text="second")],
        )
        scheduler.dispatch_ready(datetime.datetime.now())
        executor.submissions[1][2].set_result({"message_id": 2})
        scheduler.harvest_completed(datetime.datetime.now())
        executor.submissions[0][2].set_result({"message_id": 1})
        scheduler.harvest_completed(datetime.datetime.now())
        states = [OutboundTask.get_by_id(task.id).state for workflow in created for task in workflow.tasks]

    assert [args[0].id for _function, args, _future in executor.submissions] == [
        created[0].tasks[0].id,
        created[1].tasks[0].id,
    ]
    assert states == [TaskState.COMPLETED, TaskState.COMPLETED]
    assert len(adapter.finished) == 2


def _workflow_builder():
    manager = object.__new__(TelegramBotManager)
    manager.channel = SimpleNamespace(_=lambda text: text)
    return manager


def test_long_text_workflow_persists_parse_and_attachment_branches():
    manager = _workflow_builder()
    specs, result_index = manager._build_outbound_workflow_specs(
        source_key="module chat",
        slave_id="module chat",
        priority=False,
        target_chat_id=100,
        operation="send_message",
        args=(100, "x" * 5000),
        kwargs={"parse_mode": "HTML"},
        required_sender_bot_id=None,
        log_payload={"slave_message_id": "1"},
    )

    assert result_index == 0
    assert [spec.operation for spec in specs] == [
        "api_send_message",
        "api_send_message",
        "api_send_document",
        "api_send_document",
    ]
    assert specs[1].run_condition == "predecessor_error:parse_entities"
    assert specs[2].run_condition == "predecessor_success"
    assert specs[3].depends_on_step_index == 1
    assert specs[0].log_payload is not None
    assert specs[2].log_payload is None


def test_edit_workflow_persists_bounded_fallbacks_before_dispatch():
    manager = _workflow_builder()
    specs, _result_index = manager._build_outbound_workflow_specs(
        source_key="module chat",
        slave_id="module chat",
        priority=True,
        target_chat_id=100,
        operation="edit_message_text",
        args=(),
        kwargs={"chat_id": 100, "message_id": 5, "text": "updated", "parse_mode": "HTML"},
        required_sender_bot_id="777",
        log_payload={"slave_message_id": "1"},
    )

    conditions = {spec.run_condition for spec in specs}
    assert "predecessor_error:parse_entities" in conditions
    assert "predecessor_error:edit_not_allowed" in conditions
    assert "predecessor_error:edit_not_found" in conditions
    fallback_sends = [spec for spec in specs if spec.operation == "api_send_message"]
    assert fallback_sends
    assert all(spec.required_sender_bot_id is None for spec in fallback_sends)


def test_media_workflow_accounts_for_document_and_parse_fallback_calls():
    manager = _workflow_builder()
    specs, _result_index = manager._build_outbound_workflow_specs(
        source_key="module chat",
        slave_id="module chat",
        priority=False,
        target_chat_id=100,
        operation="send_audio",
        args=(100, io.BytesIO(b"audio")),
        kwargs={"caption": "caption", "parse_mode": "HTML"},
        required_sender_bot_id=None,
        log_payload=None,
    )

    assert specs[0].operation == "api_send_audio"
    assert any(
        spec.operation == "api_send_document"
        and spec.run_condition == "predecessor_error:media_bad_request"
        for spec in specs
    )
    assert sum(spec.operation.startswith("api_") for spec in specs) == len(specs)


@pytest.mark.parametrize(
    ("fallback_to_document", "expected_document_fallback"),
    [(True, True), (False, False)],
)
def test_photo_workflow_controls_document_fallback_without_leaking_internal_kwarg(
    fallback_to_document,
    expected_document_fallback,
):
    manager = _workflow_builder()
    specs, _result_index = manager._build_outbound_workflow_specs(
        source_key="module chat",
        slave_id="module chat",
        priority=False,
        target_chat_id=100,
        operation="send_photo",
        args=(100, "https://example.com/photo.jpg"),
        kwargs={
            "caption": "caption",
            "_fallback_to_document": fallback_to_document,
        },
        required_sender_bot_id=None,
        log_payload=None,
    )

    assert all("_fallback_to_document" not in spec.kwargs for spec in specs)
    assert any(spec.operation == "api_send_document" for spec in specs) is expected_document_fallback


def test_expected_error_activates_only_matching_dependency_and_updates_result(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        created = repository.create_workflow([
            _spec(text="primary"),
            OutboundTaskSpec(
                **{
                    **_spec(text="success attachment").__dict__,
                    "depends_on_step_index": 0,
                    "run_condition": "predecessor_success",
                }
            ),
            OutboundTaskSpec(
                **{
                    **_spec(text="fallback").__dict__,
                    "depends_on_step_index": 0,
                    "run_condition": "predecessor_error:parse_entities",
                }
            ),
        ])

        outcome = repository.complete_expected_error(
            created.tasks[0].id,
            error_class="parse_entities",
            last_error="can't parse entities",
            now=datetime.datetime.now(),
        )
        states = [OutboundTask.get_by_id(task.id).state for task in created.tasks]
        result_task_id = OutboundWorkflow.get_by_id(created.workflow.id).result_task_id
        final = repository.record_sent(created.tasks[2].id, {"ok": True, "message_id": 10})
        assert final.state == TaskState.SENT_PENDING_LOG
        final_outcome = repository.complete_success(created.tasks[2].id, datetime.datetime.now())

    assert outcome is None
    assert states == [TaskState.COMPLETED, TaskState.SKIPPED, TaskState.QUEUED]
    assert result_task_id == created.tasks[2].id
    assert final_outcome is not None
    assert final_outcome.state == "completed"


def test_dependency_transition_rolls_back_as_one_transaction_on_failure(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        created = repository.create_workflow([
            _spec(text="primary"),
            OutboundTaskSpec(
                **{
                    **_spec(text="dependent").__dict__,
                    "depends_on_step_index": 0,
                    "run_condition": RunCondition.PREDECESSOR_SUCCESS,
                }
            ),
        ])
        primary, dependent = created.tasks
        repository.record_sent(primary.id, {"message_id": 1})

        with patch.object(
            repository,
            "_advance_dependencies",
            side_effect=RuntimeError("dependency transition failed"),
        ):
            with pytest.raises(RuntimeError, match="dependency transition failed"):
                repository.complete_success(primary.id, datetime.datetime.now())

        primary_state = OutboundTask.get_by_id(primary.id).state
        dependent_state = OutboundTask.get_by_id(dependent.id).state
        workflow_state = OutboundWorkflow.get_by_id(created.workflow.id).state

    assert primary_state == TaskState.SENT_PENDING_LOG
    assert dependent_state == TaskState.WAITING_DEPENDENCY
    assert workflow_state == "active"


def test_live_future_heartbeat_prevents_expired_lease_recovery(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        scheduler, _executor, _adapter, created, _codec = _scheduler_fixture(
            tmp_path,
            [_spec(text="slow upload")],
            worker_count=1,
        )
        now = datetime.datetime.now()
        scheduler.dispatch_ready(now)
        OutboundTask.update(
            lease_until=now - datetime.timedelta(seconds=1)
        ).where(OutboundTask.id == created[0].tasks[0].id).execute()

        recovery = scheduler.repository.recover(
            now,
            local_in_flight_task_ids=tuple(
                item.task_id for item in scheduler.in_flight_snapshot()
            ),
        )
        assert scheduler.heartbeat(now) == 1

    assert recovery.requeued_ambiguous_ids == ()


def test_mark_in_flight_requires_matching_lease_owner(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        task = repository.create_workflow([_spec()]).tasks[0]
        now = datetime.datetime.now()
        repository.lease(
            task.id,
            lease_owner="owner-a",
            now=now,
            lease_duration=datetime.timedelta(minutes=1),
        )

        assert repository.mark_in_flight(
            task.id,
            lease_owner="owner-b",
            submitted_at=now,
        ) is False
        assert OutboundTask.get_by_id(task.id).state == TaskState.LEASED
        assert repository.mark_in_flight(
            task.id,
            lease_owner="owner-a",
            submitted_at=now,
        ) is True
        final_state = OutboundTask.get_by_id(task.id).state

    assert final_state == TaskState.IN_FLIGHT


def test_orphaned_spool_cleanup_preserves_active_media_and_removes_terminal_media(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, codec = _repository(tmp_path)
        created = repository.create_workflow([
            OutboundTaskSpec(
                **{
                    **_spec().__dict__,
                    "kwargs": {"chat_id": 100, "document": io.BytesIO(b"referenced")},
                }
            )
        ])
        referenced_name = json.loads(created.tasks[0].media_ref)[0]
        terminal = repository.create_workflow([
            OutboundTaskSpec(
                **{
                    **_spec(source_key="terminal").__dict__,
                    "kwargs": {"chat_id": 100, "document": io.BytesIO(b"terminal")},
                }
            )
        ]).tasks[0]
        terminal_name = json.loads(terminal.media_ref)[0]
        OutboundTask.update(state=TaskState.COMPLETED).where(
            OutboundTask.id == terminal.id
        ).execute()
        orphan_path = codec.spool_dir / "orphan.bin"
        orphan_path.write_bytes(b"orphan")

        removed = repository.cleanup_orphaned_spool_files()

    assert removed == 2
    assert orphan_path.exists() is False
    assert (codec.spool_dir / terminal_name).exists() is False
    assert (codec.spool_dir / referenced_name).read_bytes() == b"referenced"


def test_retried_row_precedes_same_lane_row_not_yet_submitted(tmp_path):
    class RetryAdapter(SchedulerAdapter):
        def classify_error(self, _task, _error, _selection, now):
            return FailureDisposition(FailureDisposition.RETRY, "network", retry_at=now)

    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, codec = _repository(tmp_path)
        created = [repository.create_workflow([_spec(text=text)]) for text in ("first", "second", "third")]
        executor = RecordingExecutor()
        adapter = RetryAdapter()
        scheduler = OutboundScheduler(
            repository,
            adapter,
            executor=executor,
            worker_count=2,
            lease_owner="test-process",
        )
        now = datetime.datetime.now()
        scheduler.dispatch_ready(now)
        executor.submissions[0][2].set_exception(RuntimeError("retry"))
        scheduler.harvest_completed(now)

        scheduler.dispatch_ready(now)
        submitted_ids = [args[0].id for _function, args, _future in executor.submissions]
        second_state = OutboundTask.get_by_id(created[1].tasks[0].id).state
        third_state = OutboundTask.get_by_id(created[2].tasks[0].id).state

    assert submitted_ids == [
        created[0].tasks[0].id,
        created[1].tasks[0].id,
        created[0].tasks[0].id,
    ]
    assert second_state == TaskState.IN_FLIGHT
    assert third_state == TaskState.QUEUED


def test_concurrent_sqlite_acceptance_uses_database_timestamp_and_distinct_ids(tmp_path):
    test_db = SqliteDatabase(
        tmp_path / "concurrent.db",
        pragmas={"journal_mode": "wal", "busy_timeout": 5000},
        check_same_thread=False,
    )
    models = [OutboundWorkflow, OutboundTask]
    errors = []
    task_ids = []
    barrier = threading.Barrier(6)
    with test_db.bind_ctx(models):
        test_db.create_tables(models)

        def accept(index):
            try:
                repository = OutboundRepository(OutboundPayloadCodec(tmp_path / "spool"))
                barrier.wait()
                created = repository.create_workflow([_spec(text=f"task-{index}")])
                task_ids.append(created.tasks[0].id)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=accept, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        rows = list(OutboundTask.select().order_by(OutboundTask.accepted_at, OutboundTask.id))

    test_db.close()
    assert errors == []
    assert len(set(task_ids)) == 6
    assert all(row.accepted_at is not None for row in rows)
    assert [(row.accepted_at, row.id) for row in rows] == sorted(
        (row.accepted_at, row.id) for row in rows
    )


def test_retry_after_rotates_old_row_without_cross_chat_cooldown():
    manager = object.__new__(TelegramBotManager)
    manager._bot = object()
    manager._rate_limiter = SlidingWindowRateLimiter(
        global_limit=100,
        chat_limit=100,
        safety_margin=0,
    )
    manager._bot_chat_disabled_until = {}
    manager._bot_chat_retry_failures = {}
    manager._metrics = None
    manager.logger = Mock()

    aux_limiter = SlidingWindowRateLimiter(
        global_limit=100,
        chat_limit=100,
        safety_margin=0,
        owner_id="777",
    )
    aux_bot = SimpleNamespace(
        bot_id=777,
        username="aux",
        disabled=False,
        bot=object(),
        check_membership_tri=lambda _chat_id: True,
        check_membership_sync=lambda _chat_id, timeout: True,
        peek_delay=aux_limiter.peek_delay,
        reserve_slot=aux_limiter.reserve_slot,
        release_slot=aux_limiter.release_slot,
        get_chat_send_count=lambda _chat_id: 0,
        get_known_member_chat_ids=lambda: {100, 200},
    )
    manager.bot_pool = BotPool([aux_bot], manager)
    task = SimpleNamespace(
        id=1,
        target_chat_id=100,
        required_sender_bot_id=None,
        slave_id="module chat",
        attempt_count=1,
        operation="api_send_message",
    )
    now = datetime.datetime.now()

    first = manager.select_sender(task, now).selection
    assert first is not None and first.sender_bot_id == "777"
    disposition = manager.classify_error(
        task,
        telegram.error.RetryAfter(2),
        first,
        now,
    )
    second = manager.select_sender(task, now).selection

    assert disposition.kind == FailureDisposition.RETRY
    assert second is not None and second.sender_bot_id is None
    assert ("777", 100) in manager._bot_chat_disabled_until
    assert ("777", 200) not in manager._bot_chat_disabled_until
    assert (None, 100) not in manager._bot_chat_disabled_until


def test_unknown_aux_membership_uses_bounded_recheck_without_blocking_probe():
    manager = object.__new__(TelegramBotManager)
    manager._bot = object()
    manager._rate_limiter = SlidingWindowRateLimiter(
        global_limit=100,
        chat_limit=1,
        chat_window=60,
        safety_margin=0,
    )
    manager._rate_limiter.reserve_slot(100)
    manager._bot_chat_disabled_until = {}
    manager._metrics = None
    manager.logger = Mock()
    unknown_bot = SimpleNamespace(
        bot_id=777,
        username="aux",
        disabled=False,
        bot=object(),
        check_membership_tri=Mock(return_value=None),
        check_membership_sync=Mock(return_value=False),
        peek_delay=Mock(return_value=0.0),
        reserve_slot=Mock(),
        get_chat_send_count=Mock(return_value=0),
    )
    manager.bot_pool = BotPool([unknown_bot], manager)
    task = SimpleNamespace(
        target_chat_id=100,
        required_sender_bot_id=None,
        slave_id="module chat",
    )
    now = datetime.datetime.now()

    result = manager.select_sender(task, now)

    assert result.selection is None
    assert result.retry_at is not None
    assert 0 < (result.retry_at - now).total_seconds() <= 0.25
    unknown_bot.check_membership_sync.assert_not_called()


def test_successful_send_resets_sender_chat_retry_failure_count():
    manager = object.__new__(TelegramBotManager)
    manager._bot_chat_retry_failures = {(None, 100): 3, (None, 200): 1}
    manager._outbound_live_results = {}
    manager._metrics = None
    task = SimpleNamespace(
        id=1,
        target_chat_id=100,
        accepted_at=None,
        payload="",
    )
    result = SimpleNamespace(
        chat_id=100,
        message_id=10,
        chat=SimpleNamespace(id=100),
        animation=None,
        document=None,
        video=None,
        voice=None,
        audio=None,
        sticker=None,
        video_note=None,
        photo=None,
    )
    selection = SimpleNamespace(sender_bot_id=None)

    manager.serialize_result(task, result, selection)

    assert (None, 100) not in manager._bot_chat_retry_failures
    assert manager._bot_chat_retry_failures[(None, 200)] == 1


def test_sent_pending_log_reconciliation_is_idempotent(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask, MsgLog]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        task = repository.create_workflow([_spec()]).tasks[0]
        repository.record_sent(task.id, {
            "ok": True,
            "chat_id": 100,
            "message_id": 9,
            "sender_bot_id": "777",
            "media_type": "Text",
        })
        manager = object.__new__(DatabaseManager)
        log_payload = {
            "old_message_id": None,
            "text": "hello",
            "slave_origin_uid": "module chat",
            "slave_member_uid": "module chat member",
            "msg_type": "Text",
            "sent_to": "blueset.telegram",
            "slave_message_id": "slave-message-1",
            "media_type": "Text",
            "file_id": None,
            "file_unique_id": None,
            "mime": None,
            "pickle_b64": None,
        }
        result_payload = json.loads(OutboundTask.get_by_id(task.id).result_payload)

        manager.reconcile_outbound_message_log(
            task.id, log_payload, result_payload, sender_bot_id="777",
        )
        manager.reconcile_outbound_message_log(
            task.id, log_payload, result_payload, sender_bot_id="777",
        )
        rows = list(MsgLog.select())

    assert len(rows) == 1
    assert rows[0].outbound_task_id == task.id
    assert rows[0].sender_bot_id == "777"


def test_blocking_waiter_timeout_leaves_durable_task_queued(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        manager = object.__new__(TelegramBotManager)
        manager.channel = SimpleNamespace(_=lambda text: text)
        manager._outbound_codec = OutboundPayloadCodec(tmp_path / "spool")
        manager._outbound_repository = OutboundRepository(manager._outbound_codec)
        manager._outbound_scheduler = SimpleNamespace(wake_event=threading.Event())
        manager._outbound_registry_lock = threading.Lock()
        manager._outbound_waiters = {}
        manager._outbound_waiter_receipts = {}
        manager._outbound_db_callbacks = {}
        manager._outbound_workflow_by_task = {}
        manager._outbound_live_results = {}
        manager._metrics = None
        manager._send_worker_stop = threading.Event()
        manager.logger = Mock()
        manager.BLOCKING_SEND_TIMEOUT = 0.001

        try:
            manager._enqueue_blocking_send_and_wait(
                "module chat",
                100,
                lambda *_args, **_kwargs: None,
                (manager, 100),
                {},
            )
        except RuntimeError as error:
            assert "timed out" in str(error)
        else:
            raise AssertionError("blocking waiter should time out")

        task = OutboundTask.get()

    assert task.state == TaskState.QUEUED
    assert manager._outbound_waiters == {}


def test_terminal_workflow_preserves_telegram_error_for_live_blocking_waiter():
    manager = object.__new__(TelegramBotManager)
    waiter = Future()
    telegram_error = telegram.error.BadRequest("failed to get HTTP URL content")
    manager._metrics = None
    manager._outbound_waiters = {7: waiter}
    manager._outbound_waiter_receipts = {7: True}
    manager._outbound_db_callbacks = {}
    manager._outbound_workflow_by_task = {11: 7}
    manager._outbound_live_results = {}
    manager._outbound_live_errors = {11: telegram_error}
    manager._outbound_registry_lock = threading.Lock()
    manager.channel = SimpleNamespace(chat_binding=None)

    manager.workflow_finished(WorkflowOutcome(
        workflow_id=7,
        state="dead",
        result_task_id=11,
        result_payload=None,
        error_class="bad_request",
    ))

    with pytest.raises(telegram.error.BadRequest) as raised:
        waiter.result()
    assert raised.value is telegram_error
    assert manager._outbound_live_errors == {}


def test_recovered_workflow_cleans_live_state_using_durable_task_relationship(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        created = repository.create_workflow([_spec(), _spec()])
        first_id, result_id = (task.id for task in created.tasks)
        manager = object.__new__(TelegramBotManager)
        manager._metrics = None
        manager._outbound_waiters = {}
        manager._outbound_waiter_receipts = {}
        manager._outbound_db_callbacks = {}
        manager._outbound_workflow_by_task = {}
        manager._outbound_live_results = {
            first_id: (object(), None),
            result_id: (object(), None),
        }
        manager._outbound_live_errors = {first_id: RuntimeError("terminal")}
        manager._outbound_registry_lock = threading.Lock()
        manager.channel = SimpleNamespace(chat_binding=None)

        manager.workflow_finished(WorkflowOutcome(
            workflow_id=created.workflow.id,
            state="dead",
            result_task_id=result_id,
            result_payload=None,
            error_class="terminal",
        ))

    assert manager._outbound_live_results == {}
    assert manager._outbound_live_errors == {}


def test_failed_history_workflow_remains_observable_instead_of_being_deleted():
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, HistoryMigrationEntry]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        workflow = OutboundWorkflow.create(state="dead", error_class="bad_request")
        entry = HistoryMigrationEntry.create(
            slave_chat_id="module chat",
            target_chat_id="100",
            source_master_msg_id="100.1",
            position=0,
            outbound_workflow_id=workflow.id,
            state="active",
        )

        state = DatabaseManager.reconcile_history_migration_workflow(workflow.id)
        retained = HistoryMigrationEntry.get_by_id(entry.id)

    assert state == "dead"
    assert retained.state == "dead"
    assert retained.last_error == "bad_request"


def test_chat_action_suppression_observes_durable_backlog_and_recent_aux_use(tmp_path):
    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        repository, _codec = _repository(tmp_path)
        task = repository.create_workflow([_spec()]).tasks[0]
        send_action = Mock(return_value="sent")
        decorated = TelegramBotManager.Decorators.skip_on_rate_limit(send_action)
        manager = SimpleNamespace(_aux_recent_use={})

        assert decorated(manager, 100) is None
        send_action.assert_not_called()

        OutboundTask.update(state=TaskState.COMPLETED).where(OutboundTask.id == task.id).execute()
        manager._aux_recent_use[100] = time.time()
        assert decorated(manager, 100) is None
        send_action.assert_not_called()

        manager._aux_recent_use[100] = time.time() - 10
        assert decorated(manager, 100) == "sent"

    send_action.assert_called_once_with(manager, 100)


def _public_send_manager(tmp_path, send_message, *, worker_count=1):
    manager = object.__new__(TelegramBotManager)
    manager._bot = SimpleNamespace(send_message=send_message)
    manager.bot_pool = None
    manager._rate_limiter = SlidingWindowRateLimiter(
        global_limit=100,
        chat_limit=100,
        safety_margin=0,
    )
    manager._bot_chat_disabled_until = {}
    manager._bot_chat_retry_failures = {}
    manager._send_worker_stop = threading.Event()
    manager._cleanup_tls = SimpleNamespace(pending_cleanup=[])
    manager._aux_recent_use = {}
    manager._metrics = None
    manager._outbound_waiters = {}
    manager._outbound_waiter_receipts = {}
    manager._outbound_db_callbacks = {}
    manager._outbound_workflow_by_task = {}
    manager._outbound_live_results = {}
    manager._outbound_live_errors = {}
    manager._outbound_registry_lock = threading.Lock()
    manager.channel = SimpleNamespace(_=lambda text: text, chat_binding=None)
    manager.logger = Mock()
    manager._outbound_codec = OutboundPayloadCodec(tmp_path / "spool")
    manager._outbound_repository = OutboundRepository(manager._outbound_codec)
    executor = ThreadPoolExecutor(max_workers=worker_count)
    manager._outbound_scheduler = OutboundScheduler(
        manager._outbound_repository,
        manager,
        executor=executor,
        worker_count=worker_count,
        lease_owner="test-process",
    )
    return manager, executor


def test_public_send_runs_once_through_durable_scheduler_without_internal_kwargs(tmp_path):
    test_db = SqliteDatabase(":memory:", check_same_thread=False)
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        sent_message = SimpleNamespace(
            chat_id=100,
            message_id=5,
            chat=SimpleNamespace(id=100),
        )
        send_message = Mock(return_value=sent_message)
        manager, executor = _public_send_manager(tmp_path, send_message)

        receipt = manager.send_message(
            100,
            "hello",
            _send_mode="eventual",
            _slave_id="module chat",
        )
        now = datetime.datetime.now()
        manager._outbound_scheduler.dispatch_ready(now)
        in_flight = manager._outbound_scheduler.in_flight_snapshot()
        assert len(in_flight) == 1
        in_flight[0].future.result(timeout=1)
        manager._outbound_scheduler.harvest_completed(now)
        task = OutboundTask.get()
        executor.shutdown(wait=True)

    assert receipt.queued is True
    assert task.state == TaskState.COMPLETED
    send_message.assert_called_once_with(100, "hello")
    assert manager._outbound_workflow_by_task == {}


def test_public_same_source_stream_executes_each_index_once_with_concurrent_reordering(tmp_path):
    test_db = SqliteDatabase(":memory:", check_same_thread=False)
    models = [OutboundWorkflow, OutboundTask]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        worker_count = 4
        barrier = threading.Barrier(worker_count)
        all_started = threading.Event()
        gates = {str(index): threading.Event() for index in range(worker_count)}
        finished = {str(index): threading.Event() for index in range(worker_count)}
        lock = threading.Lock()
        active = 0
        peak_active = 0
        started = []
        completed = []

        def fake_send_message(chat_id, text):
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
                started.append(text)
                if len(started) == worker_count:
                    all_started.set()
            barrier.wait(timeout=2)
            if not gates[text].wait(timeout=2):
                raise RuntimeError(f"Timed out waiting to release outbound index {text}.")
            with lock:
                completed.append(text)
                active -= 1
            finished[text].set()
            return SimpleNamespace(
                chat_id=chat_id,
                message_id=int(text) + 1,
                chat=SimpleNamespace(id=chat_id),
            )

        send_message = Mock(side_effect=fake_send_message)
        manager, executor = _public_send_manager(
            tmp_path,
            send_message,
            worker_count=worker_count,
        )
        try:
            receipts = [
                manager.send_message(
                    100,
                    str(index),
                    _send_mode="eventual",
                    _slave_id="module chat",
                )
                for index in range(worker_count)
            ]
            now = datetime.datetime.now()
            manager._outbound_scheduler.dispatch_ready(now)
            assert all_started.wait(timeout=2)
            assert len(manager._outbound_scheduler.in_flight_snapshot()) == worker_count

            for index in reversed(range(worker_count)):
                text = str(index)
                gates[text].set()
                assert finished[text].wait(timeout=2)

            manager._outbound_scheduler.harvest_completed(now)
            states = [task.state for task in OutboundTask.select().order_by(OutboundTask.id)]
        finally:
            for gate in gates.values():
                gate.set()
            executor.shutdown(wait=True)

    assert all(receipt.queued for receipt in receipts)
    assert started == ["0", "1", "2", "3"]
    assert completed == ["3", "2", "1", "0"]
    assert peak_active == worker_count
    assert send_message.call_count == worker_count
    assert sorted(call.args[1] for call in send_message.call_args_list) == ["0", "1", "2", "3"]
    assert states == [TaskState.COMPLETED] * worker_count

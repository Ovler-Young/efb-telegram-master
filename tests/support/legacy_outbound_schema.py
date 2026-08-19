from peewee import SQL, AutoField, BigIntegerField, BooleanField, DateTimeField, IntegerField, Model, TextField


def create_legacy_outbound_schema(test_database, tables=("outboundworkflow", "outboundtask")):
    class BaseModel(Model):
        class Meta:
            database = test_database

    class OutboundWorkflow(BaseModel):
        id = AutoField()
        state = TextField(default="active")
        result_task_id = BigIntegerField(null=True)
        error_class = TextField(null=True)
        created_at = DateTimeField(constraints=[SQL("DEFAULT CURRENT_TIMESTAMP")])
        completed_at = DateTimeField(null=True)

    class OutboundTask(BaseModel):
        id = AutoField()
        source_key = TextField()
        slave_id = TextField(null=True)
        priority = BooleanField(default=False)
        target_chat_id = BigIntegerField()
        message_thread_id = BigIntegerField(null=True)
        operation = TextField()
        payload = TextField()
        media_ref = TextField(null=True)
        workflow_id = BigIntegerField(index=True)
        step_index = IntegerField(default=0)
        depends_on_task_id = BigIntegerField(null=True)
        run_condition = TextField(default="always")
        result_payload = TextField(null=True)
        log_payload = TextField(null=True)
        required_sender_bot_id = TextField(null=True)
        state = TextField(default="queued")
        available_at = DateTimeField(null=True)
        lease_owner = TextField(null=True)
        lease_until = DateTimeField(null=True)
        lease_heartbeat_at = DateTimeField(null=True)
        submitted_at = DateTimeField(null=True)
        attempt_count = IntegerField(default=0)
        accepted_at = DateTimeField(constraints=[SQL("DEFAULT CURRENT_TIMESTAMP")])
        error_class = TextField(null=True)
        last_error = TextField(null=True)

        class Meta:
            indexes = (
                (("source_key", "priority", "accepted_at", "id"), False),
                (("state", "available_at"), False),
                (("workflow_id", "step_index"), True),
            )

    models = {
        "outboundworkflow": OutboundWorkflow,
        "outboundtask": OutboundTask,
    }
    test_database.create_tables([models[table] for table in tables])
    return OutboundWorkflow, OutboundTask


def create_legacy_historic_identity_source(test_database):
    test_database.execute_sql("CREATE TABLE chatassoc (id INTEGER PRIMARY KEY, master_uid TEXT NOT NULL, slave_uid TEXT NOT NULL)")
    test_database.execute_sql("CREATE TABLE topicassoc (id INTEGER PRIMARY KEY, topic_chat_id TEXT NOT NULL, message_thread_id TEXT NOT NULL, slave_uid TEXT NOT NULL)")
    test_database.execute_sql(
        "CREATE TABLE historymigrationentry (id INTEGER PRIMARY KEY, slave_chat_id TEXT NOT NULL, target_chat_id TEXT NOT NULL, "
        "message_thread_id TEXT, source_master_msg_id TEXT NOT NULL, formatted_text TEXT, media_type TEXT, source_time DATETIME, "
        "position INTEGER NOT NULL, created_at DATETIME NOT NULL)"
    )
    test_database.execute_sql("INSERT INTO chatassoc VALUES (1, 'master-old', 'slave-a'), (2, 'master-new', 'slave-a')")
    test_database.execute_sql("INSERT INTO topicassoc VALUES (1, '100', '200', 'slave-a'), (2, '101', '201', 'slave-a'), (3, '101', '201', 'slave-b')")
    test_database.execute_sql(
        "INSERT INTO historymigrationentry VALUES "
        "(1, 'slave-a', '100', NULL, '10.1', NULL, NULL, NULL, 0, CURRENT_TIMESTAMP), "
        "(2, 'slave-a', '100', NULL, '10.2', NULL, NULL, NULL, 0, CURRENT_TIMESTAMP), "
        "(3, 'slave-a', '100', '200', '10.3', NULL, NULL, NULL, 0, CURRENT_TIMESTAMP), "
        "(4, 'slave-a', '100', '200', '10.4', NULL, NULL, NULL, 0, CURRENT_TIMESTAMP)"
    )

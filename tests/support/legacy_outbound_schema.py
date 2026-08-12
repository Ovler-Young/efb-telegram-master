from peewee import SQL, AutoField, BigIntegerField, BooleanField, DateTimeField, IntegerField, Model, TextField


def legacy_outbound_models(test_database):
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

    return OutboundWorkflow, OutboundTask

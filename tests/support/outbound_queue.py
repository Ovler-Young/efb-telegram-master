from efb_telegram_master.outbound import OutboundQueue


class _Limiter:
    def peek_delay(self, _chat_id):
        return 0.0

    def try_acquire(self, _chat_id):
        return True

    def occupancy_snapshot(self):
        return {"global": 0.0, "chat": 0.0}


def _queue(sender, worker_count=2, bot_pool=None):
    queue = OutboundQueue(
        sender,
        bot_pool,
        _Limiter(),
        worker_count=worker_count,
        blocking_timeout=1,
        shutdown_drain_timeout=1,
        shutdown_join_grace=0.1,
    )
    queue.start()
    return queue

import pytest
from prometheus_client import generate_latest

from efb_telegram_master.etm_metrics import Metrics


def _render(metrics: Metrics) -> str:
    return generate_latest(metrics.registry).decode()


def test_queue_metrics_render_every_closed_matrix_event():
    metrics = Metrics()

    metrics.record_enqueued("blocking", "send_message")
    metrics.set_queue_depth(3)
    metrics.record_removal("normal", "delete_message", "terminal_discard", 1.25)
    metrics.record_dequeued("normal", "delete_message")
    metrics.record_dispatch_failure("normal", "delete_message")
    metrics.increment_in_flight("blocking", "send_message", "main")
    metrics.decrement_in_flight("blocking", "send_message", "main")
    metrics.record_completion("blocking", "send_message", "main", "failure")

    rendered = _render(metrics)

    assert 'etm_outbound_enqueued_total{operation="send_message",priority="blocking"} 1.0' in rendered
    assert "etm_outbound_queue_depth 3.0" in rendered
    assert 'etm_outbound_queue_residence_seconds_count{operation="delete_message",outcome="terminal_discard",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_queue_removals_total{operation="delete_message",outcome="terminal_discard",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_dequeued_total{operation="delete_message",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_dispatch_failures_total{operation="delete_message",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_in_flight{operation="send_message",priority="blocking",sender_kind="main"} 0.0' in rendered
    assert 'etm_outbound_completions_total{operation="send_message",outcome="failure",priority="blocking",sender_kind="main"} 1.0' in rendered


@pytest.mark.parametrize(
    "call",
    [
        lambda metrics: metrics.record_enqueued("urgent", "send_message"),
        lambda metrics: metrics.record_enqueued("normal", "get_me"),
        lambda metrics: metrics.record_removal("normal", "send_message", "requeued", 0.0),
        lambda metrics: metrics.increment_in_flight("normal", "send_message", "bot-42"),
        lambda metrics: metrics.record_completion("normal", "send_message", "main", "cancelled"),
        lambda metrics: metrics.set_queue_depth(-1),
    ],
)
def test_queue_metrics_reject_unbounded_or_invalid_values(call):
    with pytest.raises(ValueError):
        call(Metrics())

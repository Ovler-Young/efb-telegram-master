from types import SimpleNamespace
from unittest.mock import Mock

from efb_telegram_master.telegram_api import TelegramAPI


def test_answer_callback_query_does_not_forward_internal_routing_arguments() -> None:
    answer_callback_query = Mock()
    api = TelegramAPI(SimpleNamespace(), SimpleNamespace(answer_callback_query=answer_callback_query), SimpleNamespace(), None)

    api.answer_callback_query("query", text="Done", chat_id=1, message_id=2, cache_time=180)

    assert answer_callback_query.call_args.kwargs == {"text": "Done", "cache_time": 180}

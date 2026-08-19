import pytest

from efb_telegram_master.request_configuration import parse_request_configuration
from efb_telegram_master.transport.telegram_runtime import build_request


def test_request_configuration_coerces_number_and_uses_defaults() -> None:
    configuration = parse_request_configuration({"read_timeout": 6, "socket_options": [[1, 2, 3]]})

    assert configuration.connection_pool_size == 1
    assert configuration.read_timeout == 6.0
    assert configuration.http_version == "1.1"
    assert configuration.socket_options == [(1, 2, 3)]


@pytest.mark.parametrize(
    ("request_kwargs", "message"),
    [
        ({"connection_pool_size": True}, "connection_pool_size must be a positive integer"),
        ({"read_timeout": "6"}, "read_timeout must be a number or null"),
        ({"http_version": "3"}, "http_version must be one of"),
        ({"unexpected": 1}, "contains unsupported option"),
    ],
)
def test_build_request_rejects_invalid_request_options(request_kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_request(request_kwargs)


def test_request_configuration_translates_legacy_proxy_credentials() -> None:
    configuration = parse_request_configuration({"proxy_url": "http://proxy.example:8080", "username": "a user", "password": "pass word"})

    assert configuration.proxy == "http://a%20user:pass%20word@proxy.example:8080"

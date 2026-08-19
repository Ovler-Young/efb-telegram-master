import importlib

import pytest

from efb_telegram_master.request_configuration import RequestConfiguration
from efb_telegram_master.wizard_configuration import WizardConfiguration


def test_wizard_configuration_coerces_admin_ids_and_defaults_optional_sections() -> None:
    configuration = WizardConfiguration.from_mapping({"token": "token", "admins": ["1", 2]})

    assert configuration.token == "token"
    assert configuration.admins == [1, 2]
    assert configuration.flags == {}
    assert configuration.request is None
    assert configuration.rpc is None
    assert configuration.additional_sections == {}


def test_wizard_configuration_preserves_unknown_sections_and_rpc_options() -> None:
    configuration = WizardConfiguration.from_mapping(
        {
            "token": "token",
            "rpc": {"server": "localhost", "port": 8080, "authorization": "secret"},
            "database": {"path": "database.sqlite"},
        }
    )

    assert configuration.rpc is not None
    assert configuration.rpc.additional_options == {"authorization": "secret"}
    assert configuration.to_mapping()["database"] == {"path": "database.sqlite"}
    assert configuration.to_mapping()["rpc"] == {"authorization": "secret", "server": "localhost", "port": 8080}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "Config file must contain a mapping"),
        ({"token": 1}, "Telegram bot token must be a string"),
        ({"token": "token", "admins": [True]}, "Admins' user IDs must be a list of integers"),
        ({"token": "token", "flags": []}, "flags must be a mapping"),
        ({"token": "token", "request_kwargs": {"unsupported": 1}}, "request_kwargs contains unsupported option"),
    ],
)
def test_wizard_configuration_rejects_invalid_schema(data: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        WizardConfiguration.from_mapping(data)


def test_data_model_rejects_invalid_request_configuration_before_bot_construction(monkeypatch, tmp_path) -> None:
    wizard_config = importlib.import_module("efb_telegram_master.wizard_config")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("token: token\nrequest_kwargs:\n  unsupported: true\n")
    monkeypatch.setattr(wizard_config, "get_config_path", lambda channel_id: config_path)
    monkeypatch.setattr(wizard_config, "Bot", lambda **kwargs: pytest.fail("Bot must not be constructed"))

    with pytest.raises(ValueError, match="request_kwargs contains unsupported option"):
        wizard_config.DataModel("default", "")


def test_build_bot_receives_typed_request_configuration(monkeypatch, tmp_path) -> None:
    wizard_config = importlib.import_module("efb_telegram_master.wizard_config")
    telegram_runtime = importlib.import_module("efb_telegram_master.telegram_runtime")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("token: token\nrequest_kwargs:\n  read_timeout: 3\n")
    monkeypatch.setattr(wizard_config, "get_config_path", lambda channel_id: config_path)
    request_marker = object()
    received: list[RequestConfiguration] = []

    def build_request(configuration: RequestConfiguration) -> object:
        received.append(configuration)
        return request_marker

    class FakeBot:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(telegram_runtime, "build_request", build_request)
    monkeypatch.setattr(wizard_config, "Bot", FakeBot)

    model = wizard_config.DataModel("default", "")
    bot = wizard_config.build_bot(model.configuration)

    assert received == [model.configuration.request]
    assert bot.kwargs == {"token": "token", "request": request_marker}

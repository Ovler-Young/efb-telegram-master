import pytest

from efb_telegram_master.wizard_configuration import WizardConfiguration


def test_wizard_configuration_coerces_admin_ids_and_defaults_optional_sections() -> None:
    configuration = WizardConfiguration.from_mapping({"token": "token", "admins": ["1", 2]})

    assert configuration.token == "token"
    assert configuration.admins == [1, 2]
    assert configuration.flags == {}
    assert configuration.values == {"token": "token", "admins": [1, 2], "flags": {}}


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

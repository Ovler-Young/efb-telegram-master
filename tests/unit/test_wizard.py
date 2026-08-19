import importlib


def test_wizard_runs_split_setup_steps_in_order(monkeypatch):
    wizard_module = importlib.import_module("efb_telegram_master.config.wizard")
    calls = []

    class Data:
        def __init__(self, profile, instance_id):
            calls.append(("model", profile, instance_id))

        def save(self):
            calls.append("save")

    monkeypatch.setattr(wizard_module, "DataModel", Data)
    monkeypatch.setattr(wizard_module, "prerequisites_check", lambda: calls.append("prerequisites"))
    monkeypatch.setattr(wizard_module, "print_wrapped", lambda text: None)
    for name in (
        "setup_proxy",
        "setup_telegram_bot",
        "setup_telegram_bot_commands_list",
        "setup_admins",
        "setup_experimental_flags",
        "setup_network_configurations",
        "setup_rpc",
    ):
        monkeypatch.setattr(wizard_module, name, lambda data, name=name: calls.append(name))

    wizard_module.wizard("default", "instance")

    assert calls == [
        ("model", "default", "instance"),
        "prerequisites",
        "setup_proxy",
        "setup_telegram_bot",
        "setup_telegram_bot_commands_list",
        "setup_admins",
        "setup_experimental_flags",
        "setup_network_configurations",
        "setup_rpc",
        "save",
    ]

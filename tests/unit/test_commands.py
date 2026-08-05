from efb_telegram_master.commands import CommandsManager


def test_commands_module_imports_with_typed_modules_list() -> None:
    assert CommandsManager.__name__ == "CommandsManager"

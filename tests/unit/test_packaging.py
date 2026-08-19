from importlib import import_module
from pathlib import Path

from setuptools.config.pyprojecttoml import read_configuration

ROOT = Path(__file__).parents[2]
PERSISTENCE_MODULES = (
    "database_observability",
    "chat_association_repository",
    "history_migration_repository",
    "msglog_ingestion_repository",
    "msglog_repository",
    "slave_chat_info_repository",
    "slave_message_delivery_repository",
    "repository_registry",
)
TRANSPORT_MODULES = (
    "telegram_api",
    "telegram_api_operations",
    "telegram_application_lifecycle",
    "telegram_calls",
    "telegram_error_router",
    "telegram_runtime",
    "telegram_sync_bridge",
)


def test_setuptools_configuration_discovers_only_project_packages():
    configuration = read_configuration(str(ROOT / "pyproject.toml"), expand=True)
    project = configuration["project"]
    setuptools = configuration["tool"]["setuptools"]
    packages = setuptools["packages"]

    assert packages
    assert all(package == "efb_telegram_master" or package.startswith("efb_telegram_master.") for package in packages)
    assert not any(package == "tests" or package.startswith(("tests.", "build.")) for package in packages)
    assert setuptools["include-package-data"] is True
    assert list((ROOT / "efb_telegram_master" / "locale").glob("**/*.po"))
    assert project["version"] == "2.3.1"
    assert project["entry-points"]["ehforwarderbot.master"] == {"blueset.telegram": "efb_telegram_master:TelegramChannel"}
    assert project["entry-points"]["ehforwarderbot.wizard"] == {"blueset.telegram": "efb_telegram_master.wizard:wizard"}


def test_persistence_package_is_discoverable_and_importable():
    configuration = read_configuration(str(ROOT / "pyproject.toml"), expand=True)

    assert "efb_telegram_master.persistence" in configuration["tool"]["setuptools"]["packages"]
    for module in PERSISTENCE_MODULES:
        assert import_module(f"efb_telegram_master.persistence.{module}")
        assert not (ROOT / "efb_telegram_master" / f"{module}.py").exists()


def test_transport_package_is_discoverable_and_importable():
    configuration = read_configuration(str(ROOT / "pyproject.toml"), expand=True)

    assert "efb_telegram_master.transport" in configuration["tool"]["setuptools"]["packages"]
    for module in TRANSPORT_MODULES:
        assert import_module(f"efb_telegram_master.transport.{module}")
        assert not (ROOT / "efb_telegram_master" / f"{module}.py").exists()

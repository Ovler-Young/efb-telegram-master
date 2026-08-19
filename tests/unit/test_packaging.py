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
RUNTIME_MODULES = (
    "bot_manager",
    "bot_pool",
    "channel_commands",
    "channel_composition",
    "channel_locale",
    "metrics_process",
    "metrics_runtime",
    "mtproto",
    "rate_limiter",
    "rpc_utils",
)
CONFIG_MODULES = ("request", "runtime", "wizard", "wizard_configuration", "wizard_settings", "wizard_state", "wizard_steps")
CHAT_MODULES = ("chat", "chat_codec", "chat_destination_cache", "chat_head", "chat_member", "chat_object_cache", "chat_types", "topic_sync")
LINK_MODULES = ("callback_sessions", "link_actions", "link_completion", "link_service", "recipient_suggestions")
HISTORY_MODULES = ("history_replay", "msglog_ingestion", "msglog_reconstruction", "msglog_scan")
DELIVERY_MODULES = (
    "commands",
    "master_delivery",
    "master_inbound",
    "master_message",
    "master_mutations",
    "message",
    "msg_type",
    "oversized_notice",
    "slave_delivery_helpers",
    "slave_delivery_types",
    "slave_file_delivery",
    "slave_file_transfer",
    "slave_image_delivery",
    "slave_media_delivery",
    "slave_message",
    "slave_message_claims",
    "slave_routing",
    "slave_status",
    "slave_text_delivery",
)
OUTBOUND_MODULES = ("auxiliary_bot", "membership_lifecycle", "outbound", "outbound_execution", "outbound_types", "sender_policy")
CORE_MODULES = ("constants", "db", "etm_metrics", "legacy_outbound_retirement", "locale_mixin", "media", "models", "paths", "ptb_compat", "utils")


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
    assert project["entry-points"]["ehforwarderbot.wizard"] == {"blueset.telegram": "efb_telegram_master.config.wizard:wizard"}


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


def test_runtime_and_configuration_packages_are_discoverable_without_root_aliases():
    configuration = read_configuration(str(ROOT / "pyproject.toml"), expand=True)
    packages = configuration["tool"]["setuptools"]["packages"]

    for package, modules in (("runtime", RUNTIME_MODULES), ("config", CONFIG_MODULES)):
        assert f"efb_telegram_master.{package}" in packages
        for module in modules:
            assert import_module(f"efb_telegram_master.{package}.{module}")
            assert not (ROOT / "efb_telegram_master" / f"{module}.py").exists()


def test_capability_packages_are_discoverable_without_root_aliases():
    configuration = read_configuration(str(ROOT / "pyproject.toml"), expand=True)
    packages = configuration["tool"]["setuptools"]["packages"]

    for package, modules in (("chat", CHAT_MODULES), ("link", LINK_MODULES), ("history", HISTORY_MODULES), ("delivery", DELIVERY_MODULES), ("outbound", OUTBOUND_MODULES), ("core", CORE_MODULES)):
        assert f"efb_telegram_master.{package}" in packages
        for module in modules:
            assert import_module(f"efb_telegram_master.{package}.{module}")
            assert not (ROOT / "efb_telegram_master" / f"{module}.py").exists()

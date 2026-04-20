from pathlib import Path

from ehforwarderbot import coordinator
from ehforwarderbot.types import ModuleID

LOCALE_DIR = Path(__file__).resolve().parent / "locale"


def get_base_path() -> Path:
    env_data_path = __import__("os").environ.get("EFB_DATA_PATH")
    base_path = Path(env_data_path).resolve() if env_data_path else Path.home() / ".ehforwarderbot"
    base_path.mkdir(parents=True, exist_ok=True)
    return base_path


def get_data_path(module_id: ModuleID) -> Path:
    data_path = get_base_path() / "profiles" / coordinator.profile / module_id
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path


def get_config_path(module_id: ModuleID | None = None, ext: str = "yaml") -> Path:
    config_path = get_data_path(module_id) if module_id else get_base_path() / "profiles" / coordinator.profile
    config_path.mkdir(parents=True, exist_ok=True)
    return config_path / f"config.{ext}"

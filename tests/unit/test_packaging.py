from pathlib import Path

from setuptools.config.pyprojecttoml import read_configuration

ROOT = Path(__file__).parents[2]


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

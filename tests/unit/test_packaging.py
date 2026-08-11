from pathlib import Path

from setuptools.config.pyprojecttoml import read_configuration

ROOT = Path(__file__).parents[2]


def _read_configuration(project: Path) -> dict[str, object]:
    return read_configuration(str(project / "pyproject.toml"), expand=True)


def _write_project(project: Path, *, restrict_packages: bool) -> None:
    (project / "efb_telegram_master").mkdir()
    (project / "efb_telegram_master" / "__init__.py").touch()
    (project / "build" / "generated").mkdir(parents=True)
    (project / "build" / "generated" / "__init__.py").touch()
    package_filter = 'include = ["efb_telegram_master*"]\n' if restrict_packages else ""
    (project / "pyproject.toml").write_text(
        """\
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "package-discovery-test"
version = "1.0"

[tool.setuptools.packages.find]
"""
        + package_filter,
        encoding="utf-8",
    )


def test_setuptools_configuration_discovers_only_project_packages(tmp_path: Path):
    configuration = _read_configuration(ROOT)
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

    restricted_project = tmp_path / "restricted"
    unrestricted_project = tmp_path / "unrestricted"
    restricted_project.mkdir()
    unrestricted_project.mkdir()
    _write_project(restricted_project, restrict_packages=True)
    _write_project(unrestricted_project, restrict_packages=False)

    restricted_packages = _read_configuration(restricted_project)["tool"]["setuptools"]["packages"]
    unrestricted_packages = _read_configuration(unrestricted_project)["tool"]["setuptools"]["packages"]

    assert restricted_packages == ["efb_telegram_master"]
    assert any(package == "build" or package.startswith("build.") for package in unrestricted_packages)

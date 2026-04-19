import pytest
import os
from pathlib import Path
from types import ModuleType

from collections.abc import Iterator
from ruamel.yaml import YAML
from typing import List

os.environ.setdefault("PTB_TIMEDELTA", "1")

from telegram.error import NetworkError, TimedOut

import ehforwarderbot.utils
import ehforwarderbot.coordinator

from .mocks.slave import MockSlaveChannel
from .bot import get_bot
from efb_telegram_master import TelegramChannel

pytestmark = [pytest.mark.xfail(raises=TimedOut), pytest.mark.xfail(raises=NetworkError)]


@pytest.fixture(scope='session')
def bot_info():
    return get_bot()


@pytest.fixture(scope='session')
def bot_token(bot_info) -> str:
    return bot_info['token']


@pytest.fixture(scope='session')
def bot_id(bot_token) -> int:
    return int(bot_token.split(":")[0])


@pytest.fixture(scope='session')
def bot_admins(bot_info) -> List[int]:
    return bot_info['admins']


@pytest.fixture(scope='session')
def bot_admin(bot_admins) -> int:
    return bot_admins[0]


@pytest.fixture(scope='session')
def bot_groups(bot_info) -> List[int]:
    return bot_info['groups']


@pytest.fixture(scope='session')
def bot_group(bot_groups) -> int:
    return bot_groups[0]


@pytest.fixture(scope='session')
def bot_channels(bot_info) -> List[int]:
    return bot_info['channels']


@pytest.fixture(scope='session')
def bot_channel(bot_channels) -> int:
    return bot_channels[0]


@pytest.fixture(scope='session')
def bot_topic_group(bot_info):
    return bot_info['topic_group']


@pytest.fixture(scope='session')
def aux_bot_tokens(bot_info) -> List[str]:
    return bot_info['aux_bot_tokens']


@pytest.fixture(scope='session')
def aux_bot_ids(bot_info) -> List[int]:
    return bot_info['aux_bot_ids']


def dump_config(file_path: Path, data):
    """Dump YAML config to a file."""
    yaml = YAML()
    if not file_path.parent.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open('w') as file:
        yaml.dump(data, file)


def build_channel_config(bot_token, bot_admins, *, topic_group=None, aux_bot_tokens=None):
    config = {
        'token': bot_token,
        'admins': bot_admins,
    }
    if topic_group is not None:
        config['topic_group'] = topic_group
    if aux_bot_tokens:
        config['auxiliary_bots'] = [{'token': token} for token in aux_bot_tokens]
    return config


def load_test_coordinator(tmp_path_factory, monkey_class, channel_config) -> ModuleType:
    tmp_path = tmp_path_factory.mktemp("etm_test")
    monkey_class.setenv("EFB_DATA_PATH", str(tmp_path))

    config_path = ehforwarderbot.utils.get_config_path()
    dump_config(config_path, {
        "master_channel": TelegramChannel.channel_id,
        "slave_channels": ["tests.mocks.slave"],
        "middlewares": []
    })

    ehforwarderbot.coordinator.add_channel(MockSlaveChannel())

    channel_config_path = ehforwarderbot.utils.get_config_path(TelegramChannel.channel_id)
    dump_config(channel_config_path, channel_config)

    ehforwarderbot.coordinator.add_channel(TelegramChannel())

    return ehforwarderbot.coordinator


@pytest.fixture(scope="module")
def monkey_class():
    from _pytest.monkeypatch import MonkeyPatch
    mpatch = MonkeyPatch()
    yield mpatch
    mpatch.undo()


@pytest.fixture(scope="module")
def coordinator(tmp_path_factory, monkey_class, bot_token, bot_admins) -> Iterator[ModuleType]:
    """Loaded coordinator with ETM and mock modules"""
    coordinator_obj = load_test_coordinator(
        tmp_path_factory,
        monkey_class,
        build_channel_config(bot_token, bot_admins),
    )

    yield coordinator_obj

    coordinator_obj.master.stop_polling()
    for i in coordinator_obj.slaves.values():
        i.stop_polling()


@pytest.fixture(scope="module")
def coordinator_with_topic_group(tmp_path_factory, monkey_class, bot_token, bot_admins,
                                 bot_topic_group) -> Iterator[ModuleType]:
    if bot_topic_group is None:
        pytest.skip("TOPIC_GROUP is not configured")

    coordinator_obj = load_test_coordinator(
        tmp_path_factory,
        monkey_class,
        build_channel_config(bot_token, bot_admins, topic_group=bot_topic_group),
    )

    yield coordinator_obj

    coordinator_obj.master.stop_polling()
    for i in coordinator_obj.slaves.values():
        i.stop_polling()


@pytest.fixture(scope="module")
def coordinator_with_auxiliary_bots(tmp_path_factory, monkey_class, bot_token, bot_admins,
                                    aux_bot_tokens) -> Iterator[ModuleType]:
    if not aux_bot_tokens:
        pytest.skip("AUX_BOT_TOKEN is not configured")

    coordinator_obj = load_test_coordinator(
        tmp_path_factory,
        monkey_class,
        build_channel_config(bot_token, bot_admins, aux_bot_tokens=aux_bot_tokens),
    )

    yield coordinator_obj

    coordinator_obj.master.stop_polling()
    for i in coordinator_obj.slaves.values():
        i.stop_polling()


@pytest.fixture(scope="module")
def channel(coordinator) -> TelegramChannel:
    return coordinator.master


@pytest.fixture(scope="module")
def slave(coordinator) -> MockSlaveChannel:
    return coordinator.slaves[MockSlaveChannel.channel_id]


@pytest.fixture(scope="module")
def channel_with_topic_group(coordinator_with_topic_group) -> TelegramChannel:
    return coordinator_with_topic_group.master


@pytest.fixture(scope="module")
def slave_with_topic_group(coordinator_with_topic_group) -> MockSlaveChannel:
    return coordinator_with_topic_group.slaves[MockSlaveChannel.channel_id]


@pytest.fixture(scope="module")
def channel_with_auxiliary_bots(coordinator_with_auxiliary_bots) -> TelegramChannel:
    return coordinator_with_auxiliary_bots.master


@pytest.fixture(scope="module")
def slave_with_auxiliary_bots(coordinator_with_auxiliary_bots) -> MockSlaveChannel:
    return coordinator_with_auxiliary_bots.slaves[MockSlaveChannel.channel_id]


# Isolation of unit tests and integration tests

def pytest_addoption(parser):
    parser.addoption(
        "--mode",
        action="store",
        metavar="MODE",
        default='unit',
        choices=['unit', 'integration', 'both'],
        help="run test of mode 'unit', 'integration', or 'both' (default: 'unit').",
    )


def pytest_configure(config):
    # register an additional marker
    config.addinivalue_line("markers", "unit: mark test as a unit test")
    config.addinivalue_line("markers", "integration: mark test as an integration test")


def pytest_runtest_setup(item):
    is_unit = item.module.__name__.startswith('tests.unit')
    is_integration = item.module.__name__.startswith('tests.integration')
    mode = item.config.getoption("--mode")
    if mode == 'unit' and not is_unit:
        pytest.skip("test is a unit test", allow_module_level=True)
    elif mode == 'integration' and not is_integration:
        pytest.skip("test is an integration test", allow_module_level=True)

    if is_integration:
        item.fixturenames.append('poll_bot')

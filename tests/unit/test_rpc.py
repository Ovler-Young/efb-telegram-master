import time
from types import SimpleNamespace

from pytest import fixture

from efb_telegram_master.rpc_utils import RPCUtilities


@fixture(scope="module")
def rpc(channel):
    return channel.rpc_utilities


def test_rpc_channels_id(rpc, coordinator):
    assert set(coordinator.slaves.keys()) == set(rpc.get_slave_channels_ids())


def test_configured_rpc_server_starts_and_shutdown_is_repeatable():
    utilities = RPCUtilities(SimpleNamespace(config={"rpc": {"server": "127.0.0.1", "port": 0}}, db=SimpleNamespace()))

    assert utilities.thread is not None
    assert utilities.thread.is_alive()

    started = time.monotonic()
    utilities.shutdown()
    utilities.shutdown()

    assert time.monotonic() - started < 2
    assert not utilities.thread.is_alive()

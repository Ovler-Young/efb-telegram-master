import socket
import threading
import time

from pytest import fixture

from efb_telegram_master.config.runtime import RPCConfiguration
from efb_telegram_master.runtime.rpc_utils import RPCShutdownTimeout, RPCUtilities


class FakeDatabase:
    pass


class FakeCoordinator:
    def __init__(self) -> None:
        self.slaves = {}


@fixture(scope="module")
def rpc(channel):
    return channel.rpc_utilities


def test_rpc_channels_id(rpc, coordinator):
    assert set(coordinator.slaves.keys()) == set(rpc.get_slave_channels_ids())


def test_configured_rpc_server_requires_explicit_start_and_stops_idempotently():
    database = FakeDatabase()
    coordinator_module = FakeCoordinator()
    utilities = RPCUtilities(RPCConfiguration(port=0), database, coordinator_module)

    assert not hasattr(database, "config")
    assert not hasattr(coordinator_module, "db")

    assert utilities.thread is None
    utilities.start()
    assert utilities.thread is not None
    assert utilities.thread.is_alive()

    assert utilities.stop(time.monotonic() + 1) == ()
    assert utilities.stop(time.monotonic() + 1) == ()

    assert not utilities.thread.is_alive()


def test_rpc_stop_reports_an_active_handler_until_a_retry_joins_it():
    utilities = RPCUtilities(RPCConfiguration(port=0), FakeDatabase(), FakeCoordinator())
    utilities.start()
    assert utilities.server is not None

    started = threading.Event()
    original_finish_request = utilities.server.finish_request

    def finish_request(request, client_address):
        started.set()
        original_finish_request(request, client_address)

    utilities.server.finish_request = finish_request
    client = socket.create_connection(utilities.server.server_address, timeout=1)
    try:
        client.sendall(b"POST /RPC2 HTTP/1.1\r\nContent-Length: 100\r\n\r\n<methodCall>")
        assert started.wait(1)

        errors = utilities.stop(time.monotonic() + 0.02)

        assert len(errors) == 1
        assert isinstance(errors[0], RPCShutdownTimeout)
        assert errors[0].active_handlers == 1
    finally:
        client.close()

    assert utilities.stop(time.monotonic() + 1) == ()

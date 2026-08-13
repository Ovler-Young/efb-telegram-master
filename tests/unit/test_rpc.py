import socket
import threading
import time
from types import SimpleNamespace

from pytest import fixture

from efb_telegram_master.rpc_utils import RPCShutdownTimeout, RPCUtilities


@fixture(scope="module")
def rpc(channel):
    return channel.rpc_utilities


def test_rpc_channels_id(rpc, coordinator):
    assert set(coordinator.slaves.keys()) == set(rpc.get_slave_channels_ids())


def test_configured_rpc_server_requires_explicit_start_and_stops_idempotently():
    utilities = RPCUtilities(SimpleNamespace(config={"rpc": {"server": "127.0.0.1", "port": 0}}, db=SimpleNamespace()))

    assert utilities.server is None
    assert utilities.thread is None

    utilities.start()

    assert utilities.thread is not None
    assert utilities.thread.is_alive()
    assert utilities.stop(time.monotonic() + 1) == ()
    assert utilities.stop(time.monotonic() + 1) == ()
    assert not utilities.thread.is_alive()


def test_rpc_shutdown_retains_live_handler_until_a_retry_joins_it():
    utilities = RPCUtilities(SimpleNamespace(config={"rpc": {"server": "127.0.0.1", "port": 0}}, db=SimpleNamespace()))
    utilities.start()
    assert utilities.server is not None
    assert utilities.thread is not None
    assert utilities.server.daemon_threads is False

    request_started = threading.Event()
    original_finish_request = utilities.server.finish_request

    def finish_request(request, client_address):
        request_started.set()
        original_finish_request(request, client_address)

    utilities.server.finish_request = finish_request
    client = socket.create_connection(utilities.server.server_address, timeout=1)
    client.sendall(b"POST /RPC2 HTTP/1.1\r\nContent-Length: 100\r\n\r\n<methodCall>")
    assert request_started.wait(1)

    errors = utilities.stop(time.monotonic() + 0.02)
    assert len(errors) == 1
    assert isinstance(errors[0], RPCShutdownTimeout)
    assert errors[0].active_handlers == 1
    assert not utilities.thread.is_alive()

    with socket.socket() as rejected_client:
        rejected_client.settimeout(0.2)
        try:
            rejected_client.connect(utilities.server.server_address)
        except OSError:
            pass
        else:
            raise AssertionError("RPC listener accepted a request after shutdown began")

    client.close()
    assert utilities.stop(time.monotonic() + 1) == ()

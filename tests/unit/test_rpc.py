from pytest import fixture


@fixture(scope="module")
def rpc(channel):
    return channel.rpc_utilities


def test_rpc_channels_id(rpc, coordinator):
    assert set(coordinator.slaves.keys()) == set(rpc.get_slave_channels_ids())


def test_rpc_server_serves_and_stops():
    import threading
    from types import SimpleNamespace
    from xmlrpc.client import ServerProxy, Transport

    class TimeoutTransport(Transport):
        def make_connection(self, host):
            connection = super().make_connection(host)
            connection.timeout = 2
            return connection

    from efb_telegram_master.rpc_utils import RPCUtilities

    rpc = RPCUtilities(SimpleNamespace(
        config={"rpc": {"server": "127.0.0.1", "port": 0}},
        db=SimpleNamespace(),
    ))
    try:
        with ServerProxy("http://%s:%s" % rpc.server.server_address,
                         transport=TimeoutTransport()) as client:
            assert "system.listMethods" in client.system.listMethods()
    finally:
        stopper = threading.Thread(target=rpc.shutdown, daemon=True)
        stopper.start()
        stopper.join(timeout=2)
    assert not stopper.is_alive()
    assert not rpc._server_thread.is_alive()
    assert rpc.server.socket.fileno() == -1

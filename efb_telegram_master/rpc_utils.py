import threading
from typing import TYPE_CHECKING, List, Optional
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

from ehforwarderbot import coordinator

if TYPE_CHECKING:
    from . import TelegramChannel


class RPCUtilities:
    """Useful functions exposed to RPC server"""

    def __init__(self, channel: "TelegramChannel"):
        self.channel = channel
        self.server: Optional[SimpleXMLRPCServer] = None
        self.thread: Optional[threading.Thread] = None
        self._shutdown_lock = threading.Lock()
        self._stopped = False

        rpc_config = self.channel.config.get("rpc")
        if not rpc_config:
            return

        # Restrict to a particular path.
        class RequestHandler(SimpleXMLRPCRequestHandler):
            rpc_paths = ("/", "/RPC2")

        server_addr = rpc_config["server"]
        port = rpc_config["port"]

        self.server = SimpleXMLRPCServer((server_addr, port), requestHandler=RequestHandler)

        self.server.register_introspection_functions()
        self.server.register_multicall_functions()
        self.server.register_instance(self.channel.db)
        self.server.register_function(self.get_slave_channels_ids)

        self.thread = threading.Thread(target=self.server.serve_forever, name="ETM RPC server thread", daemon=True)
        self.thread.start()

    def shutdown(self):
        """Shutdown RPC server if running."""
        with self._shutdown_lock:
            if self._stopped:
                return
            self._stopped = True
            server, thread = self.server, self.thread
        if server is not None:
            if thread is not None and thread.is_alive():
                server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    @staticmethod
    def get_slave_channels_ids() -> List[str]:
        """Get the collection of slave channel IDs in current instance"""
        return list(coordinator.slaves.keys())

    # TODO: add more utilities that could be useful for RPC?

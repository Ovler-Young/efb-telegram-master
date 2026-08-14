import threading
import time
from collections.abc import Mapping
from socketserver import ThreadingMixIn
from typing import TYPE_CHECKING, List, Optional, Protocol, TypedDict
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

from ehforwarderbot.types import ModuleID

if TYPE_CHECKING:
    from .db import DatabaseManager


class RPCConfig(TypedDict):
    server: str
    port: int


class SlaveChannelCoordinator(Protocol):
    @property
    def slaves(self) -> Mapping[ModuleID, object]: ...


class RPCShutdownTimeout(TimeoutError):
    """Raised when an RPC server still owns a request at its shutdown deadline."""

    def __init__(self, active_handlers: int, server_thread_alive: bool) -> None:
        self.active_handlers = active_handlers
        self.server_thread_alive = server_thread_alive
        super().__init__(f"RPC shutdown timed out with {active_handlers} active handler(s) and server_thread_alive={server_thread_alive}.")


class _ThreadedXMLRPCServer(ThreadingMixIn, SimpleXMLRPCServer):
    daemon_threads = False
    block_on_close = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._request_lock = threading.Lock()
        self._accepting_requests = True
        self._listener_closed = False
        self._active_handler_threads: set[threading.Thread] = set()

    def stop_accepting_requests(self) -> None:
        with self._request_lock:
            self._accepting_requests = False

    def process_request(self, request, client_address) -> None:
        with self._request_lock:
            if not self._accepting_requests:
                self.shutdown_request(request)
                return
            thread = threading.Thread(target=self.process_request_thread, args=(request, client_address))
            self._active_handler_threads.add(thread)
            # Keep registration and start in one critical section.  Otherwise a
            # shutdown can observe the registered thread before it is alive,
            # omit it from the join, and allow it to run after resource teardown.
            thread.start()

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._request_lock:
                self._active_handler_threads.discard(threading.current_thread())

    def close_listener(self) -> None:
        with self._request_lock:
            if self._listener_closed:
                return
            self._listener_closed = True
        SimpleXMLRPCServer.server_close(self)

    def request_shutdown(self) -> None:
        # BaseServer.shutdown() waits indefinitely for serve_forever(); the
        # lifecycle owner enforces the absolute deadline instead.
        self._BaseServer__shutdown_request = True

    def join_handlers(self, deadline: float) -> int:
        while True:
            with self._request_lock:
                active_threads = tuple(self._active_handler_threads)
            live_threads = tuple(thread for thread in active_threads if thread.is_alive())
            if not live_threads:
                return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return len(live_threads)
            live_threads[0].join(remaining)


class RPCUtilities:
    """Functions exposed through the optional XML-RPC server."""

    def __init__(self, rpc_config: Optional[RPCConfig], database: "DatabaseManager", coordinator_module: SlaveChannelCoordinator):
        self._rpc_config = rpc_config
        self._database = database
        self._coordinator_module = coordinator_module
        self.server: Optional[_ThreadedXMLRPCServer] = None
        self.thread: Optional[threading.Thread] = None
        self._shutdown_lock = threading.Lock()
        self._started = False
        self._stopped = False

    def start(self) -> None:
        """Bind and start the RPC server after its collaborators exist."""
        with self._shutdown_lock:
            if self._started:
                return
            if self._stopped:
                raise RuntimeError("Cannot restart a stopped RPC server.")

            rpc_config = self._rpc_config
            if not rpc_config:
                self._started = True
                return

            class RequestHandler(SimpleXMLRPCRequestHandler):
                rpc_paths = ("/", "/RPC2")

            server = _ThreadedXMLRPCServer((rpc_config["server"], rpc_config["port"]), requestHandler=RequestHandler)
            try:
                server.register_introspection_functions()
                server.register_multicall_functions()
                server.register_instance(self._database)
                server.register_function(self.get_slave_channels_ids)
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, name="ETM RPC server thread")
                thread.start()
            except BaseException:
                server.server_close()
                raise

            self.server = server
            self.thread = thread
            self._started = True

    def stop(self, deadline: float) -> tuple[BaseException, ...]:
        """Stop accepting work and join all RPC threads before ``deadline``."""
        with self._shutdown_lock:
            if self._stopped:
                return ()
            server, thread = self.server, self.thread
            if server is None:
                self._stopped = True
                return ()

            server.stop_accepting_requests()
            server.request_shutdown()
            server.close_listener()

            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
            active_handlers = server.join_handlers(deadline)
            server_thread_alive = thread is not None and thread.is_alive()
            if active_handlers or server_thread_alive:
                return (RPCShutdownTimeout(active_handlers, server_thread_alive),)

            self._stopped = True
            return ()

    def get_slave_channels_ids(self) -> List[str]:
        """Get the collection of slave channel IDs in current instance."""
        return list(self._coordinator_module.slaves.keys())

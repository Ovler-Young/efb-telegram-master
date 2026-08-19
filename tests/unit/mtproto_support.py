from pathlib import Path
from types import SimpleNamespace

from efb_telegram_master.runtime.mtproto import MTProtoConfig


class FakeClient:
    def __init__(self, session_path: Path, _config: MTProtoConfig):
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.requests: list[object] = []
        self.session_path = session_path

    async def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True
        self.session_path.with_suffix(".session").touch()

    async def start(self, *, bot_token: str) -> None:
        assert bot_token == "bot-token"

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        return SimpleNamespace(messages=[SimpleNamespace(id=message_id) for message_id in request.id])

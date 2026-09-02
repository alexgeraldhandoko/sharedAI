import pytest
from fastapi import WebSocketDisconnect

from app.main import WebSocketHub


pytestmark = pytest.mark.anyio


class DisconnectingWebSocket:
    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        raise WebSocketDisconnect(code=1006)


async def test_broadcast_removes_a_socket_that_disconnects_while_sending() -> None:
    hub = WebSocketHub()
    websocket = DisconnectingWebSocket()
    await hub.connect("123456", websocket, "Alex")  # type: ignore[arg-type]

    await hub.broadcast("123456", {"type": "presence.updated"})
    hub.disconnect("123456", websocket, "Alex")  # type: ignore[arg-type]

    assert hub.members("123456") == []
    assert "123456" not in hub._connections

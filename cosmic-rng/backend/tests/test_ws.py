"""WebSocket: authentication, origin enforcement and event delivery."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import login


async def test_ws_rejects_foreign_origin(app):
    with TestClient(app) as tc:
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect("/ws", headers={"Origin": "https://evil.example"}) as ws:
                ws.receive_json()
        assert exc.value.code == 4403


async def test_ws_hello_and_personal_events(app):
    p = await login(app)
    cookie = p.c.cookies.get("crng_session")
    with TestClient(app) as tc:
        tc.cookies.set("crng_session", cookie)
        with tc.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
            hello = ws.receive_json()
            assert hello["t"] == "hello" and hello["d"]["authenticated"] and hello["d"]["user_id"] == p.id
            ws.send_json({"t": "ping"})
            msg = ws.receive_json()
            while msg["t"] != "pong":
                msg = ws.receive_json()
            # an event published for this user reaches this socket
            import asyncio

            from app.core.pubsub import bus

            tc.portal.call(bus.publish, "user", "notify", {"title": "hi"}, p.id)
            msg = ws.receive_json()
            while msg["t"] != "notify":
                msg = ws.receive_json()
            assert msg["d"]["title"] == "hi"
            _ = asyncio


async def test_ws_anonymous_gets_public_feed_only(app):
    with TestClient(app) as tc:
        with tc.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
            hello = ws.receive_json()
            assert hello["t"] == "hello" and not hello["d"]["authenticated"] and hello["d"]["user_id"] is None

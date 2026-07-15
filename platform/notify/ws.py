"""Browser-compatible first-message authentication and websocket fan-out."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


@dataclass(frozen=True)
class _Connection:
    websocket: WebSocket
    loop: asyncio.AbstractEventLoop


class WebSocketHub:
    """Process-local live delivery; durable rows remain the source of truth."""

    def __init__(self) -> None:
        self._connections: dict[str, list[_Connection]] = {}
        self._lock = Lock()

    def add(self, actor: str, websocket: WebSocket) -> _Connection:
        connection = _Connection(websocket, asyncio.get_running_loop())
        with self._lock:
            self._connections.setdefault(actor, []).append(connection)
        return connection

    def remove(self, actor: str, connection: _Connection) -> None:
        with self._lock:
            rows = self._connections.get(actor, [])
            if connection in rows:
                rows.remove(connection)
            if not rows:
                self._connections.pop(actor, None)

    def push(self, actor: str, payload: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._connections.get(actor, []))
        for connection in targets:
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            try:
                if current is connection.loop:
                    connection.loop.create_task(connection.websocket.send_json(payload))
                else:
                    future = asyncio.run_coroutine_threadsafe(
                        connection.websocket.send_json(payload), connection.loop
                    )
                    future.result(timeout=5)
            except Exception:  # noqa: BLE001 - the durable row remains available
                self.remove(actor, connection)


def install_websocket(app: FastAPI, hub: WebSocketHub) -> None:
    """Install the sole websocket route with first-message bearer verification."""

    @app.websocket("/console/ops/ws")
    async def notification_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        connection: _Connection | None = None
        actor: str | None = None
        try:
            first = await websocket.receive_json()
            token = first.get("token") if isinstance(first, dict) else None
            verifier = getattr(app.state, "console_verifier", None)
            if not isinstance(token, str) or not token or verifier is None:
                await websocket.close(code=4401)
                return
            try:
                claims = verifier.verify(token)
            except Exception:  # noqa: BLE001 - every verifier failure is untrusted
                await websocket.close(code=4401)
                return
            identities = getattr(app.state, "console_identities", {})
            roles = getattr(app.state, "console_roles", {})
            actor = identities.get(f"{claims.tid}:{claims.oid}")
            if not isinstance(actor, str) or actor not in roles:
                await websocket.close(code=4401)
                return
            connection = hub.add(actor, websocket)
            await websocket.send_json({"type": "ready", "actor": actor})
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            if actor is not None and connection is not None:
                hub.remove(actor, connection)


__all__ = ["WebSocketHub", "install_websocket"]

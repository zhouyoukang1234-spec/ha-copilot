"""Async Home Assistant client: the bridge's connection to a live HA instance.

Wraps both transports HA exposes:
- the REST API (``/api/...``) for states, services, history, templates, config; and
- the WebSocket API (``/api/websocket``) for everything the UI uses that REST
  does not cover - the area / device / entity / label / floor / category
  registries, Lovelace dashboards, users, config entries and system health.

One persistent authenticated WebSocket is kept open; a background reader
dispatches results to the matching request by id, so many tools can issue
WS commands concurrently. Everything is intentionally thin and general: typed
helpers for the common surface plus ``rest`` / ``ws`` escape hatches so no part
of Home Assistant is unreachable.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import aiohttp


class HAError(RuntimeError):
    """A Home Assistant REST/WS call returned an error."""


class HAClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or os.environ.get("HA_BASE_URL", "http://localhost:8123")).rstrip("/")
        self.token = token or os.environ.get("HA_TOKEN", "")
        if not self.token:
            raise HAError("no HA token; set HA_TOKEN or pass token=")
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._ws_lock = asyncio.Lock()

    # ---- lifecycle -------------------------------------------------------
    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"}
            )
        return self._session

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    # ---- REST ------------------------------------------------------------
    async def rest(self, method: str, path: str, json: Any = None) -> Any:
        """Raw REST call. ``path`` is e.g. ``/api/states``."""
        session = await self._ensure_session()
        url = self.base_url + path
        async with session.request(method, url, json=json) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise HAError(f"{method} {path} -> HTTP {resp.status}: {text[:300]}")
            if not text:
                return None
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return await_json(text)
            return text

    # ---- WebSocket -------------------------------------------------------
    async def _ensure_ws(self) -> aiohttp.ClientWebSocketResponse:
        if self._ws is not None and not self._ws.closed:
            return self._ws
        async with self._ws_lock:
            if self._ws is not None and not self._ws.closed:
                return self._ws
            session = await self._ensure_session()
            ws = await session.ws_connect(self.base_url + "/api/websocket", heartbeat=30)
            msg = await ws.receive_json()
            if msg.get("type") != "auth_required":
                raise HAError(f"unexpected WS greeting: {msg}")
            await ws.send_json({"type": "auth", "access_token": self.token})
            msg = await ws.receive_json()
            if msg.get("type") != "auth_ok":
                raise HAError(f"WS auth failed: {msg}")
            self._ws = ws
            self._reader = asyncio.create_task(self._read_loop(ws))
            return ws

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = msg.json()
                fut = self._pending.pop(data.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(data)
        except asyncio.CancelledError:  # noqa: PERF203
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(HAError("WS connection closed"))
            self._pending.clear()

    async def ws(self, type_: str, **payload: Any) -> Any:
        """Send a WS command and return its ``result`` (raising on error)."""
        ws = await self._ensure_ws()
        self._ws_id += 1
        msg_id = self._ws_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await ws.send_json({"id": msg_id, "type": type_, **payload})
        data = await asyncio.wait_for(fut, timeout=30)
        if not data.get("success", False):
            raise HAError(f"ws {type_} failed: {data.get('error')}")
        return data.get("result")


def await_json(text: str) -> Any:
    import json

    return json.loads(text)

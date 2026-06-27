"""A public, native MCP endpoint served by Home Assistant itself.

Speaks MCP over HTTP (JSON-RPC 2.0) at ``/api/ha_copilot/mcp``. Authentication
is Home Assistant's own (bearer token / logged-in session), so the same
endpoint is both a deep in-process control plane *and* a public interface any
external MCP client (Devin, or any third-party platform) can call.

Implements the subset of MCP that tool clients need: ``initialize``,
``tools/list``, ``tools/call``, ``ping`` and the ``notifications/*`` no-ops.
Responses are plain ``application/json`` JSON-RPC, which Streamable-HTTP MCP
clients accept.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DATA_STORE, DOMAIN
from .mcp_tools import TOOL_SPECS, async_call_tool

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ha_copilot", "version": "0.2.0"}


def _rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class CopilotMCPView(HomeAssistantView):
    """Native MCP (JSON-RPC over HTTP) endpoint."""

    url = "/api/ha_copilot/mcp"
    name = "api:ha_copilot:mcp"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Discovery helper (not part of MCP): advertise the endpoint."""
        return self.json(
            {
                "server": SERVER_INFO,
                "protocolVersion": PROTOCOL_VERSION,
                "transport": "streamable-http (json-rpc)",
                "tools": [t["name"] for t in TOOL_SPECS],
            }
        )

    async def post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return self.json(_rpc_error(None, -32700, "parse error"), status_code=400)

        if isinstance(body, list):
            responses = [await self._handle(request, m) for m in body]
            responses = [r for r in responses if r is not None]
            return self.json(responses)

        response = await self._handle(request, body)
        if response is None:
            # Notification: no JSON-RPC response body.
            return web.Response(status=202)
        return self.json(response)

    async def _handle(self, request: web.Request, msg: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(msg, dict):
            return _rpc_error(None, -32600, "invalid request")
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        # Notifications (no id) -> no response.
        if req_id is None and method and method.startswith("notifications/"):
            return None

        if method == "initialize":
            return _rpc_result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": SERVER_INFO,
                "capabilities": {"tools": {"listChanged": False}},
            })
        if method == "ping":
            return _rpc_result(req_id, {})
        if method == "tools/list":
            return _rpc_result(req_id, {"tools": TOOL_SPECS})
        if method == "tools/call":
            return await self._tools_call(request, req_id, params)
        if req_id is None:
            return None
        return _rpc_error(req_id, -32601, f"method not found: {method}")

    async def _tools_call(self, request: web.Request, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        store = self.hass.data[DOMAIN][DATA_STORE]
        user = request["hass_user"]
        try:
            result = await async_call_tool(self.hass, store, user, name, args)
        except Exception as err:  # noqa: BLE001 - report as an MCP tool error
            _LOGGER.warning("ha_copilot MCP tool '%s' failed: %s", name, err)
            return _rpc_result(req_id, {
                "isError": True,
                "content": [{"type": "text", "text": f"{type(err).__name__}: {err}"}],
            })
        structured = result if isinstance(result, dict) else {"result": result}
        return _rpc_result(req_id, {
            "content": [{"type": "text", "text": json.dumps(result, default=str, ensure_ascii=False)}],
            "structuredContent": structured,
            "isError": False,
        })

"""HTTP surface for HA-Copilot — the capability layer, no model involved.

Two foundations are exposed over authenticated HTTP, both backed by the *same*
deterministic tool layer (:mod:`tools`):

* Direct  — ``GET  /api/ha_copilot/tools``    list the tool catalog
            ``POST /api/ha_copilot/run_tool`` run one tool: {tool, args}
* MCP     — ``POST /api/ha_copilot/mcp``      a minimal MCP (JSON-RPC 2.0) server
            speaking ``initialize`` / ``tools/list`` / ``tools/call`` so any MCP
            client (the external agent) can operate Home Assistant.

The agent is always external; this component never calls an inference endpoint.
"""
from __future__ import annotations

from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_dumps

from . import tools
from .const import (
    API_MCP,
    API_RUN_TOOL,
    API_TOOLS,
    CONF_ALLOW_RESTART,
    CONF_ALLOW_WRITE,
    DATA_STORE,
    DOMAIN,
)

MCP_PROTOCOL_VERSION = "2024-11-05"


def _mcp_tools() -> list[dict[str, Any]]:
    """Convert the OpenAI-style tool specs into MCP tool descriptors."""
    out: list[dict[str, Any]] = []
    for spec in tools.TOOL_SPECS:
        fn = spec.get("function", {})
        out.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


class CopilotConfigView(HomeAssistantView):
    """Expose the capability surface (no secrets, no model) for the panel."""

    url = "/api/ha_copilot/config"
    name = "api:ha_copilot:config"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        store = self.hass.data[DOMAIN][DATA_STORE]
        return self.json(
            {
                "allow_write": store.get(CONF_ALLOW_WRITE),
                "allow_restart": store.get(CONF_ALLOW_RESTART),
                "tool_count": len(tools.TOOL_SPECS),
                "mcp_endpoint": API_MCP,
            }
        )


class CopilotToolsView(HomeAssistantView):
    """List the deterministic tool catalog."""

    url = API_TOOLS
    name = "api:ha_copilot:tools"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        return self.json({"tools": _mcp_tools()})


class CopilotRunToolView(HomeAssistantView):
    """Run a single tool deterministically (no LLM)."""

    url = API_RUN_TOOL
    name = "api:ha_copilot:run_tool"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)
        tool = (body or {}).get("tool")
        if not tool or not isinstance(tool, str):
            return self.json({"error": "'tool' is required"}, status_code=400)
        args = (body or {}).get("args") or {}
        store = self.hass.data[DOMAIN][DATA_STORE]
        result = await tools.dispatch(self.hass, store, tool, args)
        return self.json({"tool": tool, "result": result})


class CopilotMcpView(HomeAssistantView):
    """A minimal MCP server (JSON-RPC 2.0) over the tool layer.

    Supports ``initialize``, ``tools/list`` and ``tools/call`` — enough for an
    MCP client to discover and operate the full Home Assistant tool surface.
    """

    url = API_MCP
    name = "api:ha_copilot:mcp"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        try:
            req = await request.json()
        except ValueError:
            return self.json(_rpc_error(None, -32700, "parse error"), status_code=400)

        # Support both a single request and a JSON-RPC batch.
        if isinstance(req, list):
            replies = [await self._handle(item) for item in req]
            return self.json([r for r in replies if r is not None])
        reply = await self._handle(req)
        return self.json(reply if reply is not None else {})

    async def _handle(self, req: Any) -> dict[str, Any] | None:
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "invalid request")
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        # Notifications (no id) get no response body.
        if method == "notifications/initialized":
            return None

        if method == "initialize":
            return _rpc_ok(
                rid,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "ha-copilot", "version": "0.2.0"},
                },
            )
        if method == "ping":
            return _rpc_ok(rid, {})
        if method == "tools/list":
            return _rpc_ok(rid, {"tools": _mcp_tools()})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if not name:
                return _rpc_error(rid, -32602, "missing tool name")
            store = self.hass.data[DOMAIN][DATA_STORE]
            result = await tools.dispatch(self.hass, store, name, args)
            is_error = isinstance(result, dict) and "error" in result
            return _rpc_ok(
                rid,
                {
                    "content": [
                        # Use HA's JSON encoder (not stdlib) so any result the
                        # HTTP run_tool path can serialise — Context, datetime,
                        # registry objects — also works over MCP.
                        {"type": "text", "text": json_dumps(result)}
                    ],
                    "isError": is_error,
                },
            )
        return _rpc_error(rid, -32601, f"method not found: {method}")


def _rpc_ok(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

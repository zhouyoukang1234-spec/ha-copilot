"""HTTP surface for HA-Copilot — the capability layer, no model involved.

Two foundations are exposed over authenticated HTTP, both backed by the *same*
deterministic tool layer (:mod:`tools`):

* Direct  — ``GET  /api/ha_copilot/tools``    list the tool catalog
            ``POST /api/ha_copilot/run_tool`` run one tool: {tool, args}
* MCP     — ``POST /api/ha_copilot/mcp``      a minimal MCP (JSON-RPC 2.0) server
            speaking ``initialize`` / ``tools/list`` / ``tools/call`` so any MCP
            client (the external agent) can operate Home Assistant. The same
            server is also reachable over the standard MCP **HTTP+SSE** transport
            (``GET /api/ha_copilot/mcp/sse`` + ``POST .../mcp/messages``) so
            off-the-shelf clients (Claude Desktop, Cline, ...) connect as-is.

The agent is always external; this component never calls an inference endpoint.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_dumps

from . import tools
from .const import (
    API_MCP,
    API_MCP_MESSAGES,
    API_MCP_SSE,
    API_RUN_TOOL,
    API_TOOLS,
    CONF_ALLOW_RESTART,
    CONF_ALLOW_WRITE,
    DATA_MCP_SESSIONS,
    DATA_STORE,
    DOMAIN,
)

MCP_PROTOCOL_VERSION = "2024-11-05"
# How long the SSE stream waits for a queued message before emitting a comment
# keep-alive (seconds), so proxies don't drop an idle connection.
MCP_SSE_KEEPALIVE = 25.0


def _mcp_tools() -> list[dict[str, Any]]:
    """Convert the OpenAI-style tool specs into MCP tool descriptors."""
    out: list[dict[str, Any]] = []
    for spec in tools.TOOL_SPECS:
        fn = spec.get("function", {})
        name = fn.get("name", "")
        out.append(
            {
                "name": name,
                "description": fn.get("description", ""),
                "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
                # MCP tool annotations: let clients flag destructive ops and
                # surface read-only tools safely (single source: tools module).
                "annotations": tools.tool_annotations(name),
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
                "mcp_sse_endpoint": API_MCP_SSE,
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
            replies = [await _dispatch_rpc(self.hass, item) for item in req]
            return self.json([r for r in replies if r is not None])
        reply = await _dispatch_rpc(self.hass, req)
        return self.json(reply if reply is not None else {})


class CopilotMcpSseView(HomeAssistantView):
    """Standard MCP HTTP+SSE transport — the SSE half (server -> client).

    Opens a ``text/event-stream``, immediately announces the message endpoint
    via an ``endpoint`` event (per the MCP 2024-11-05 spec), then relays every
    JSON-RPC reply produced by :class:`CopilotMcpMessagesView` for this session.
    """

    url = API_MCP_SSE
    name = "api:ha_copilot:mcp:sse"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        session_id = uuid.uuid4().hex
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        sessions = self.hass.data[DOMAIN].setdefault(DATA_MCP_SESSIONS, {})
        sessions[session_id] = queue

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        try:
            endpoint = f"{API_MCP_MESSAGES}?session_id={session_id}"
            await response.write(
                f"event: endpoint\ndata: {endpoint}\n\n".encode()
            )
            while True:
                try:
                    msg = await asyncio.wait_for(
                        queue.get(), timeout=MCP_SSE_KEEPALIVE
                    )
                except TimeoutError:
                    await response.write(b": keep-alive\n\n")
                    continue
                if msg is None:
                    break
                await response.write(f"event: message\ndata: {msg}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            sessions.pop(session_id, None)
        return response


class CopilotMcpMessagesView(HomeAssistantView):
    """Standard MCP HTTP+SSE transport — the message half (client -> server).

    Receives JSON-RPC over POST, dispatches it against the same tool layer, and
    pushes the reply onto the matching SSE session's queue. Returns ``202`` with
    no body, as the SSE transport requires.
    """

    url = API_MCP_MESSAGES
    name = "api:ha_copilot:mcp:messages"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        session_id = request.query.get("session_id")
        sessions = self.hass.data[DOMAIN].get(DATA_MCP_SESSIONS, {})
        queue = sessions.get(session_id)
        if queue is None:
            return self.json(
                _rpc_error(None, -32600, "unknown or expired session"),
                status_code=404,
            )
        try:
            req = await request.json()
        except ValueError:
            return self.json(_rpc_error(None, -32700, "parse error"), status_code=400)

        if isinstance(req, list):
            replies = [await _dispatch_rpc(self.hass, item) for item in req]
            payload = [r for r in replies if r is not None]
            if payload:
                await queue.put(json_dumps(payload))
        else:
            reply = await _dispatch_rpc(self.hass, req)
            if reply is not None:
                await queue.put(json_dumps(reply))
        return web.Response(status=202)


async def _dispatch_rpc(hass: HomeAssistant, req: Any) -> dict[str, Any] | None:
    """Handle one JSON-RPC request against the tool layer (transport-agnostic)."""
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
        store = hass.data[DOMAIN][DATA_STORE]
        result = await tools.dispatch(hass, store, name, args)
        is_error = isinstance(result, dict) and "error" in result
        return _rpc_ok(
            rid,
            {
                "content": [
                    # Use HA's JSON encoder (not stdlib) so any result the HTTP
                    # run_tool path can serialise — Context, datetime, registry
                    # objects — also works over MCP.
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

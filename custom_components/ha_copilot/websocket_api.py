"""WebSocket API for HA-Copilot.

Registers WebSocket commands so the HA frontend (and any WS client) can
invoke deterministic tools, list the catalog, and query resource hub
data — all through HA's native real-time transport. This completes the
fifth (and most native) access route alongside HA services, MCP, HTTP,
and the LLM API.

Commands:
  ha_copilot/tools       — list the tool catalog
  ha_copilot/run_tool    — invoke a single deterministic tool
  ha_copilot/info        — integration status summary
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_ALLOW_RESTART,
    CONF_ALLOW_WRITE,
    DATA_STORE,
    DOMAIN,
)
from .tools import TOOL_SPECS, dispatch


def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register all HA-Copilot WebSocket commands."""
    websocket_api.async_register_command(hass, ws_tools)
    websocket_api.async_register_command(hass, ws_run_tool)
    websocket_api.async_register_command(hass, ws_info)


@callback
@websocket_api.websocket_command(
    {vol.Required("type"): "ha_copilot/tools"}
)
def ws_tools(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the full tool catalog."""
    tools = []
    for spec in TOOL_SPECS:
        func = spec.get("function", {})
        tools.append({
            "name": func.get("name", ""),
            "description": func.get("description", ""),
        })
    connection.send_result(msg["id"], {"tools": tools, "count": len(tools)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ha_copilot/run_tool",
        vol.Required("tool"): str,
        vol.Optional("args", default={}): dict,
    }
)
@websocket_api.async_response
async def ws_run_tool(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Invoke a single deterministic tool and return the result."""
    store = hass.data.get(DOMAIN, {}).get(DATA_STORE, {})
    tool_name = msg["tool"]
    args = msg.get("args", {})
    try:
        result = await dispatch(hass, store, tool_name, args)
        hass.bus.async_fire(
            f"{DOMAIN}_tool_called",
            {"tool": tool_name, "ok": result.get("ok", True) if isinstance(result, dict) else True},
        )
        connection.send_result(msg["id"], result)
    except Exception as exc:  # noqa: BLE001
        connection.send_error(
            msg["id"], "tool_error", str(exc)
        )


@callback
@websocket_api.websocket_command(
    {vol.Required("type"): "ha_copilot/info"}
)
def ws_info(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return integration status summary."""
    store = hass.data.get(DOMAIN, {}).get(DATA_STORE, {})
    connection.send_result(msg["id"], {
        "version": "0.3.0",
        "tool_count": len(TOOL_SPECS),
        "data_sources": 9,
        "routes": [
            "HA services",
            "MCP",
            "Native LLM API",
            "HTTP",
            "WebSocket",
        ],
        "allow_write": store.get(CONF_ALLOW_WRITE, True),
        "allow_restart": store.get(CONF_ALLOW_RESTART, False),
    })

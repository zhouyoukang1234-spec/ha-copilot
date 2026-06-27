"""The native MCP tool surface exposed by HA-Copilot.

Two layers, one home:
  * Ergonomic, well-typed tools backed by Home Assistant's in-process Python
    APIs (state machine, registries, template, recorder, config files) -- via
    :func:`.tools.dispatch`.
  * A universal ``ha_ws`` escape hatch that runs *any* Home Assistant WebSocket
    command in-process (registries CRUD, Lovelace, automation/scene config,
    config entries, auth, system health, ...). A few high-traffic ws commands
    are also surfaced as named tools for convenience.

This is what an external agent (Devin, or any third-party MCP client) talks to:
deep native fusion *and* a public, standard tool interface. 道法自然.
"""
from __future__ import annotations

from typing import Any

from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant

from .tools import dispatch as _dispatch_native
from .ws_exec import WSCommandError, async_ws_execute

# ws command behind each convenience list tool.
_WS_LIST_TOOLS: dict[str, str] = {
    "list_dashboards": "lovelace/dashboards/list",
    "list_floors": "config/floor_registry/list",
    "list_labels": "config/label_registry/list",
    "list_devices": "config/device_registry/list",
    "list_entities": "config/entity_registry/list",
    "list_users": "config/auth/list",
    "list_config_entries": "config_entries/get",
}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


# MCP tool advertisements (name -> description, inputSchema).
TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "list_states", "description": "List entities and their current state; optional domain filter (e.g. 'light').",
     "inputSchema": _obj({"domain": {"type": "string"}})},
    {"name": "get_state", "description": "Full state + attributes of one entity.",
     "inputSchema": _obj({"entity_id": {"type": "string"}}, ["entity_id"])},
    {"name": "list_services", "description": "List callable services; optional domain filter.",
     "inputSchema": _obj({"domain": {"type": "string"}})},
    {"name": "call_service", "description": "Call any HA service. Pass target as 'entity_id'; extra params in 'data'.",
     "inputSchema": _obj({"domain": {"type": "string"}, "service": {"type": "string"},
                          "entity_id": {"type": "string"}, "data": {"type": "object"}},
                         ["domain", "service"])},
    {"name": "render_template", "description": "Render a Jinja2 template against live HA state.",
     "inputSchema": _obj({"template": {"type": "string"}}, ["template"])},
    {"name": "get_history", "description": "Recorded state changes for an entity over the last N hours.",
     "inputSchema": _obj({"entity_id": {"type": "string"}, "hours": {"type": "integer"}}, ["entity_id"])},
    {"name": "check_config", "description": "Validate the HA configuration files.",
     "inputSchema": _obj({})},
    {"name": "registry_overview", "description": "Counts of entities, devices and areas.",
     "inputSchema": _obj({})},
    {"name": "list_areas", "description": "List all areas (rooms/zones).",
     "inputSchema": _obj({})},
    {"name": "create_area", "description": "Create an area (idempotent).",
     "inputSchema": _obj({"name": {"type": "string"}}, ["name"])},
    {"name": "rename_entity", "description": "Rename a registry-backed entity.",
     "inputSchema": _obj({"entity_id": {"type": "string"}, "name": {"type": "string"}}, ["entity_id", "name"])},
    {"name": "assign_entity_area", "description": "Assign an entity to an area (by id or name).",
     "inputSchema": _obj({"entity_id": {"type": "string"}, "area": {"type": "string"}}, ["entity_id", "area"])},
    {"name": "set_entity_enabled", "description": "Enable/disable a registry-backed entity.",
     "inputSchema": _obj({"entity_id": {"type": "string"}, "enabled": {"type": "boolean"}}, ["entity_id", "enabled"])},
    {"name": "create_automation", "description": "Create an automation (pass 'automation' object or alias/trigger/action fields).",
     "inputSchema": _obj({"automation": {"type": "object"}})},
    {"name": "create_scene", "description": "Create a scene from an entities->state mapping.",
     "inputSchema": _obj({"name": {"type": "string"}, "entities": {"type": "object"}}, ["name", "entities"])},
    {"name": "create_script", "description": "Create a script from an action sequence.",
     "inputSchema": _obj({"alias": {"type": "string"}, "sequence": {"type": "array"}}, ["alias"])},
    {"name": "read_config_file", "description": "Read a text file inside the HA config dir.",
     "inputSchema": _obj({"path": {"type": "string"}}, ["path"])},
    {"name": "write_config_file", "description": "Write a text file inside the HA config dir (guarded by allow_write).",
     "inputSchema": _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])},
    {"name": "read_logs", "description": "Tail the Home Assistant log.",
     "inputSchema": _obj({"lines": {"type": "integer"}})},
    {"name": "reload", "description": "Reload a domain's YAML config (or 'all').",
     "inputSchema": _obj({"domain": {"type": "string"}}, ["domain"])},
    # convenience ws-backed list tools
    {"name": "list_dashboards", "description": "List Lovelace dashboards.", "inputSchema": _obj({})},
    {"name": "list_floors", "description": "List floors.", "inputSchema": _obj({})},
    {"name": "list_labels", "description": "List labels.", "inputSchema": _obj({})},
    {"name": "list_devices", "description": "List devices.", "inputSchema": _obj({})},
    {"name": "list_entities", "description": "List entity-registry entries.", "inputSchema": _obj({})},
    {"name": "list_users", "description": "List Home Assistant users.", "inputSchema": _obj({})},
    {"name": "list_config_entries", "description": "List integration config entries.", "inputSchema": _obj({})},
    {"name": "system_health", "description": "System health info for all integrations.", "inputSchema": _obj({})},
    # universal escape hatch -- the whole WebSocket surface, in-process.
    {"name": "ha_ws", "description": "Run ANY Home Assistant WebSocket command in-process. "
        "command_type e.g. 'config/area_registry/create'; payload is the command's params.",
     "inputSchema": _obj({"command_type": {"type": "string"}, "payload": {"type": "object"}}, ["command_type"])},
]


async def async_call_tool(
    hass: HomeAssistant, store: dict, user: User, name: str, args: dict[str, Any]
) -> Any:
    """Execute a native MCP tool and return its raw result."""
    if name == "ha_ws":
        return await async_ws_execute(
            hass, user, args["command_type"], args.get("payload") or {}
        )
    if name == "system_health":
        # system_health/info streams per-domain results over the socket; use the
        # integration's native aggregator for a single complete snapshot.
        from homeassistant.components import system_health

        return await system_health.get_info(hass)
    if name in _WS_LIST_TOOLS:
        return await async_ws_execute(hass, user, _WS_LIST_TOOLS[name], {})
    # Everything else is a native python tool.
    result = await _dispatch_native(hass, store, name, args)
    if isinstance(result, dict) and "error" in result and len(result) == 1:
        raise WSCommandError(result["error"])
    return result

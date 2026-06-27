"""HA-MCP: a Model Context Protocol server that plugs an external agent (Devin)
into the *entire* user-operable surface of a Home Assistant instance.

This is the "Cursor for Home Assistant" plumbing: a strong agent on one side, a
complete-but-thin control layer over HA on the other. Tools cover states &
services, automations / scenes / scripts, the area / device / entity / label /
floor registries, Lovelace dashboards, helpers, history & logbook, templates,
config validation, users, integrations and system health - plus ``ha_rest`` /
``ha_ws`` escape hatches so nothing in HA is out of reach.
"""
from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import HAClient

mcp = FastMCP("ha-mcp")
_client: HAClient | None = None


def client() -> HAClient:
    global _client
    if _client is None:
        _client = HAClient()
    return _client


def _loads(s: str | None, default: Any) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)


# ====================================================================== #
# Discovery / overview
# ====================================================================== #
@mcp.tool()
async def ha_overview() -> dict:
    """High-level snapshot of the instance: HA version/location, entity count
    per domain, and the number of areas/devices. Start here to orient."""
    cfg = await client().rest("GET", "/api/config")
    states = await client().rest("GET", "/api/states")
    by_domain: dict[str, int] = {}
    for s in states:
        by_domain[s["entity_id"].split(".")[0]] = by_domain.get(s["entity_id"].split(".")[0], 0) + 1
    areas = await client().ws("config/area_registry/list")
    devices = await client().ws("config/device_registry/list")
    return {
        "version": cfg.get("version"),
        "location_name": cfg.get("location_name"),
        "time_zone": cfg.get("time_zone"),
        "state_count": len(states),
        "domains": dict(sorted(by_domain.items())),
        "area_count": len(areas),
        "device_count": len(devices),
        "components_loaded": len(cfg.get("components", [])),
    }


@mcp.tool()
async def get_config() -> dict:
    """Full core configuration (units, location, version, loaded components)."""
    return await client().rest("GET", "/api/config")


@mcp.tool()
async def check_config() -> dict:
    """Validate the YAML configuration (errors block a restart). Run after edits."""
    return await client().rest("POST", "/api/config/core/check_config")


# ====================================================================== #
# States & services
# ====================================================================== #
@mcp.tool()
async def list_states(domain: str = "") -> list[dict]:
    """List entity states, optionally filtered to one ``domain`` (e.g. 'light').
    Returns entity_id, state and friendly_name for each."""
    states = await client().rest("GET", "/api/states")
    out = []
    for s in states:
        if domain and not s["entity_id"].startswith(domain + "."):
            continue
        out.append({
            "entity_id": s["entity_id"],
            "state": s["state"],
            "name": s["attributes"].get("friendly_name"),
        })
    return out


@mcp.tool()
async def get_state(entity_id: str) -> dict:
    """Full state object (state + all attributes + timestamps) for one entity."""
    return await client().rest("GET", f"/api/states/{entity_id}")


@mcp.tool()
async def set_state(entity_id: str, state: str, attributes: str = "") -> dict:
    """Directly set an entity's state in the state machine (does not call a
    device). ``attributes`` is an optional JSON object string. Useful for
    template sources and test fixtures."""
    body: dict[str, Any] = {"state": state}
    attrs = _loads(attributes, {})
    if attrs:
        body["attributes"] = attrs
    return await client().rest("POST", f"/api/states/{entity_id}", json=body)


@mcp.tool()
async def list_services(domain: str = "") -> Any:
    """List callable services. With ``domain`` set, returns just that domain's
    services and their fields; otherwise returns the domain names."""
    services = await client().rest("GET", "/api/services")
    if domain:
        for d in services:
            if d["domain"] == domain:
                return d["services"]
        return {"error": f"domain '{domain}' has no services"}
    return sorted(d["domain"] for d in services)


@mcp.tool()
async def call_service(domain: str, service: str, data: str = "", target: str = "") -> Any:
    """Call any service, e.g. domain='light' service='turn_on'. ``data`` and
    ``target`` are optional JSON object strings (target carries entity_id /
    area_id / device_id). Returns the resulting states of any targeted entities."""
    payload = _loads(data, {})
    tgt = _loads(target, {})
    # The REST API takes entity_id/area_id/device_id flat in the body (it has no
    # nested 'target' selector like the WS API), so merge target into the body.
    if tgt:
        payload = {**payload, **tgt}
    return await client().rest("POST", f"/api/services/{domain}/{service}", json=payload)


@mcp.tool()
async def render_template(template: str) -> str:
    """Render a Jinja2 template against live state (e.g.
    "{{ states('sensor.x') }}"). The single best probe for HA's data model."""
    return await client().rest("POST", "/api/template", json={"template": template})


# ====================================================================== #
# History, logbook, logs
# ====================================================================== #
@mcp.tool()
async def get_history(entity_id: str, hours: int = 24) -> list:
    """Recorded state changes for an entity over the last ``hours``."""
    import datetime
    start = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).isoformat()
    return await client().rest(
        "GET", f"/api/history/period/{start}?filter_entity_id={entity_id}&minimal_response"
    ) or []


@mcp.tool()
async def get_logbook(hours: int = 24, entity_id: str = "") -> list:
    """Human-readable logbook of what happened over the last ``hours`` (optionally
    for one entity)."""
    import datetime
    start = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).isoformat()
    path = f"/api/logbook/{start}"
    if entity_id:
        path += f"?entity={entity_id}"
    return await client().rest("GET", path) or []


@mcp.tool()
async def get_error_log() -> str:
    """The raw HA error log - the first place to look when something misbehaves."""
    return await client().rest("GET", "/api/error_log")


# ====================================================================== #
# Area / floor / device / entity / label registries (WebSocket)
# ====================================================================== #
@mcp.tool()
async def list_areas() -> list:
    """All areas (rooms/zones) with their ids, names, floors and labels."""
    return await client().ws("config/area_registry/list")


@mcp.tool()
async def create_area(name: str, floor_id: str = "") -> Any:
    """Create an area. Returns the new area record (with ``area_id``)."""
    payload: dict[str, Any] = {"name": name}
    if floor_id:
        payload["floor_id"] = floor_id
    return await client().ws("config/area_registry/create", **payload)


@mcp.tool()
async def update_area(area_id: str, changes: str) -> Any:
    """Update an area. ``changes`` is a JSON object string (name, floor_id, labels...)."""
    return await client().ws("config/area_registry/update", area_id=area_id, **_loads(changes, {}))


@mcp.tool()
async def delete_area(area_id: str) -> Any:
    """Delete an area by id."""
    return await client().ws("config/area_registry/delete", area_id=area_id)


@mcp.tool()
async def list_floors() -> list:
    """All floors (groupings of areas)."""
    return await client().ws("config/floor_registry/list")


@mcp.tool()
async def create_floor(name: str, level: int = 0) -> Any:
    """Create a floor at an optional ``level``."""
    return await client().ws("config/floor_registry/create", name=name, level=level)


@mcp.tool()
async def list_devices() -> list:
    """All devices in the device registry (id, name, manufacturer, area, model)."""
    devices = await client().ws("config/device_registry/list")
    return [
        {
            "id": d["id"],
            "name": d.get("name_by_user") or d.get("name"),
            "manufacturer": d.get("manufacturer"),
            "model": d.get("model"),
            "area_id": d.get("area_id"),
        }
        for d in devices
    ]


@mcp.tool()
async def update_device(device_id: str, changes: str) -> Any:
    """Update a device. ``changes`` JSON object string: name_by_user, area_id, disabled_by."""
    return await client().ws("config/device_registry/update", device_id=device_id, **_loads(changes, {}))


@mcp.tool()
async def list_entities(domain: str = "") -> list:
    """Entity-registry entries (the persistent identity behind each entity:
    unique_id, area, labels, enabled/hidden state). Optional ``domain`` filter."""
    ents = await client().ws("config/entity_registry/list")
    if domain:
        ents = [e for e in ents if e["entity_id"].startswith(domain + ".")]
    return ents


@mcp.tool()
async def get_entity(entity_id: str) -> Any:
    """Full entity-registry record for one entity."""
    return await client().ws("config/entity_registry/get", entity_id=entity_id)


@mcp.tool()
async def update_entity(entity_id: str, changes: str) -> Any:
    """Update an entity-registry entry. ``changes`` is a JSON object string and may
    contain: name, icon, area_id, labels (list), new_entity_id (rename id),
    disabled_by (null to enable), hidden_by (null to unhide)."""
    return await client().ws("config/entity_registry/update", entity_id=entity_id, **_loads(changes, {}))


@mcp.tool()
async def remove_entity(entity_id: str) -> Any:
    """Delete an entity-registry entry (only works for removable entities)."""
    return await client().ws("config/entity_registry/remove", entity_id=entity_id)


@mcp.tool()
async def list_labels() -> list:
    """All labels (cross-cutting tags applied to entities/devices/areas)."""
    return await client().ws("config/label_registry/list")


@mcp.tool()
async def create_label(name: str, color: str = "", icon: str = "") -> Any:
    """Create a label with an optional color and icon."""
    payload: dict[str, Any] = {"name": name}
    if color:
        payload["color"] = color
    if icon:
        payload["icon"] = icon
    return await client().ws("config/label_registry/create", **payload)


# ====================================================================== #
# Automations / scenes / scripts (config API)
# ====================================================================== #
@mcp.tool()
async def list_automations() -> list:
    """All automations with their numeric config id, entity_id and on/off state."""
    states = await client().rest("GET", "/api/states")
    out = []
    for s in states:
        if s["entity_id"].startswith("automation."):
            out.append({
                "entity_id": s["entity_id"],
                "id": s["attributes"].get("id"),
                "name": s["attributes"].get("friendly_name"),
                "state": s["state"],
            })
    return out


@mcp.tool()
async def get_automation(config_id: str) -> Any:
    """Editable config of one automation (by its numeric ``id`` from list_automations)."""
    return await client().rest("GET", f"/api/config/automation/config/{config_id}")


@mcp.tool()
async def save_automation(config_id: str, config: str) -> Any:
    """Create or overwrite an automation. ``config`` is a JSON object string with
    keys alias / trigger / condition / action (mode optional). HA persists it to
    automations.yaml and reloads. Pick a fresh numeric ``config_id`` to create."""
    res = await client().rest("POST", f"/api/config/automation/config/{config_id}", json=_loads(config, {}))
    return {"saved": config_id, "result": res}


@mcp.tool()
async def delete_automation(config_id: str) -> Any:
    """Delete an automation by its numeric config id."""
    return await client().rest("DELETE", f"/api/config/automation/config/{config_id}")


@mcp.tool()
async def get_scene(scene_id: str) -> Any:
    """Editable config of one scene (by id)."""
    return await client().rest("GET", f"/api/config/scene/config/{scene_id}")


@mcp.tool()
async def save_scene(scene_id: str, config: str) -> Any:
    """Create/overwrite a scene. ``config`` JSON object string: name + entities map."""
    res = await client().rest("POST", f"/api/config/scene/config/{scene_id}", json=_loads(config, {}))
    return {"saved": scene_id, "result": res}


@mcp.tool()
async def get_script(object_id: str) -> Any:
    """Editable config of one script (``object_id`` is the part after 'script.')."""
    return await client().rest("GET", f"/api/config/script/config/{object_id}")


@mcp.tool()
async def save_script(object_id: str, config: str) -> Any:
    """Create/overwrite a script (entity script.<object_id>). ``config`` JSON object
    string with a 'sequence' list (and optional alias)."""
    res = await client().rest("POST", f"/api/config/script/config/{object_id}", json=_loads(config, {}))
    return {"saved": object_id, "result": res}


# ====================================================================== #
# Helpers (input_*, timer, counter, ...) via WS storage collections
# ====================================================================== #
@mcp.tool()
async def list_helpers(helper_domain: str) -> list:
    """List helpers of a domain, e.g. 'input_boolean', 'input_number', 'counter',
    'timer', 'input_select', 'input_text', 'input_datetime'."""
    return await client().ws(f"{helper_domain}/list")


@mcp.tool()
async def create_helper(helper_domain: str, config: str) -> Any:
    """Create a helper. ``config`` is a JSON object string (e.g. {"name": "...",
    "min": 0, "max": 100} for input_number). Returns the created record."""
    return await client().ws(f"{helper_domain}/create", **_loads(config, {}))


# ====================================================================== #
# Lovelace dashboards
# ====================================================================== #
@mcp.tool()
async def list_dashboards() -> list:
    """All Lovelace dashboards registered in storage mode."""
    return await client().ws("lovelace/dashboards/list")


@mcp.tool()
async def get_dashboard(url_path: str = "") -> Any:
    """Get a dashboard's full card/view config. Empty ``url_path`` = default dashboard."""
    payload = {"url_path": url_path} if url_path else {}
    return await client().ws("lovelace/config", **payload)


@mcp.tool()
async def save_dashboard(config: str, url_path: str = "") -> Any:
    """Overwrite a dashboard's config. ``config`` is a JSON object string with a
    'views' list. Empty ``url_path`` = default dashboard."""
    payload: dict[str, Any] = {"config": _loads(config, {})}
    if url_path:
        payload["url_path"] = url_path
    await client().ws("lovelace/config/save", **payload)
    return {"saved": url_path or "(default)"}


# ====================================================================== #
# Users / integrations / system
# ====================================================================== #
@mcp.tool()
async def list_users() -> list:
    """All Home Assistant users (id, name, owner/admin flags, active)."""
    return await client().ws("config/auth/list")


@mcp.tool()
async def list_config_entries() -> list:
    """All configured integrations (config entries) with domain, title and state."""
    return await client().ws("config_entries/get")


@mcp.tool()
async def system_health() -> dict:
    """Aggregated system-health info reported by each integration (empty if the
    system_health integration reports nothing)."""
    return await client().ws("system_health/info") or {}


# ====================================================================== #
# Universal escape hatches
# ====================================================================== #
@mcp.tool()
async def ha_rest(method: str, path: str, json_body: str = "") -> Any:
    """Call any HA REST endpoint directly. ``path`` starts with '/api/'. Use when
    no typed tool fits."""
    return await client().rest(method.upper(), path, json=_loads(json_body, None))


@mcp.tool()
async def ha_ws(command_type: str, payload: str = "") -> Any:
    """Send any HA WebSocket command. ``payload`` is a JSON object string of extra
    fields. Use for any UI capability not covered by a typed tool."""
    return await client().ws(command_type, **_loads(payload, {}))


def main() -> None:
    # HA_MCP_TRANSPORT=stdio (default, for local/registered clients) or
    # 'streamable-http' (to expose a remote MCP endpoint other agents register).
    transport = os.environ.get("HA_MCP_TRANSPORT", "stdio")
    if transport in ("http", "streamable-http"):
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()

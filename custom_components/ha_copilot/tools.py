"""Deep-fusion tool layer: the operations HA-Copilot can perform on Home Assistant.

Each tool is a thin, well-typed wrapper around a Home Assistant internal API
(state machine, service registry, registries, config files, config check). The
LLM agent selects and invokes these via OpenAI-style function calling; this is
the layer that makes the AI "fused" with HA rather than calling it from outside.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import timedelta
from typing import Any

import voluptuous as vol  # noqa: F401  (kept for future schema validation)
import yaml

from homeassistant.components.recorder import get_instance as _recorder_get_instance
from homeassistant.components.recorder import history as _recorder_history
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.check_config import async_check_ha_config_file
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util

from .const import CONF_ALLOW_RESTART, CONF_ALLOW_WRITE


def _safe_path(hass: HomeAssistant, rel_path: str) -> str:
    """Resolve a config-relative path, refusing to escape the config dir."""
    base = os.path.realpath(hass.config.config_dir)
    target = os.path.realpath(os.path.join(base, rel_path))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f"path '{rel_path}' escapes the config directory")
    return target


async def _list_states(hass: HomeAssistant, domain: str | None = None) -> dict[str, Any]:
    states = hass.states.async_all(domain) if domain else hass.states.async_all()
    items = [
        {
            "entity_id": s.entity_id,
            "state": s.state,
            "name": s.attributes.get("friendly_name"),
        }
        for s in sorted(states, key=lambda s: s.entity_id)
    ]
    return {"count": len(items), "entities": items[:400]}


def _resolve_entity_id(
    hass: HomeAssistant, query: str, domain_hint: str | None = None
) -> tuple[str | None, list[dict[str, Any]]]:
    """Best-effort resolve an LLM/user entity reference to a real entity_id.

    Small models routinely pass a friendly name ("客厅灯") or a half-built id
    ("light.客厅") instead of the exact entity_id. Rather than bounce every such
    call back, resolve it: returns ``(entity_id, [])`` on an unambiguous match,
    or ``(None, candidates)`` with plausible suggestions for the agent to retry.
    """
    if hass.states.get(query) is not None:
        return query, []
    q = query.strip()
    prefix_domain: str | None = None
    token = q
    if "." in q:
        maybe_domain, rest = q.split(".", 1)
        if maybe_domain.replace("_", "").isalnum() and maybe_domain.isascii():
            prefix_domain, token = maybe_domain, rest
    domain = prefix_domain or domain_hint
    states = hass.states.async_all(domain) if domain else hass.states.async_all()
    ql = token.lower().strip()

    def _name(s: Any) -> str:
        return (s.attributes.get("friendly_name") or "").lower()

    # 1) exact entity_id, case-insensitive.
    for s in states:
        if s.entity_id.lower() == q.lower():
            return s.entity_id, []
    # 2) exact friendly name.
    exact = [s for s in states if _name(s) == ql]
    if len(exact) == 1:
        return exact[0].entity_id, []
    # 3) substring either direction on name, or on the id's object part.
    subs = [
        s
        for s in states
        if ql
        and (ql in _name(s) or (_name(s) and _name(s) in ql) or ql in s.entity_id.split(".")[-1])
    ]
    if len(subs) == 1:
        return subs[0].entity_id, []
    pool = exact or subs or list(states)
    candidates = [
        {"entity_id": s.entity_id, "name": s.attributes.get("friendly_name")}
        for s in sorted(pool, key=lambda s: s.entity_id)[:10]
    ]
    return None, candidates


async def _get_state(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    s = hass.states.get(entity_id)
    if s is None:
        resolved, candidates = _resolve_entity_id(hass, entity_id)
        if resolved:
            s = hass.states.get(resolved)
        else:
            return {"error": f"entity '{entity_id}' not found", "candidates": candidates}
    return {
        "entity_id": s.entity_id,
        "state": s.state,
        "attributes": dict(s.attributes),
        "last_changed": s.last_changed.isoformat(),
    }


async def _list_services(hass: HomeAssistant, domain: str | None = None) -> dict[str, Any]:
    svcs = hass.services.async_services()
    if domain:
        return {"domain": domain, "services": sorted(svcs.get(domain, {}).keys())}
    return {"domains": {d: sorted(s.keys()) for d, s in sorted(svcs.items())}}


async def _call_service(
    hass: HomeAssistant, domain: str, service: str, data: dict | None = None
) -> dict[str, Any]:
    if not hass.services.has_service(domain, service):
        return {"error": f"service '{domain}.{service}' does not exist"}
    data = dict(data or {})
    # Validate/resolve any referenced entity_ids so the agent gets corrective
    # feedback - or an automatic correction - instead of silently calling a
    # service against a non-existent entity.
    raw = data.get("entity_id")
    ids: list[str] = []
    if isinstance(raw, str):
        ids = [raw]
    elif isinstance(raw, (list, tuple)):
        ids = [str(e) for e in raw]

    resolved_ids: list[str] = []
    corrections: dict[str, str] = {}
    for e in ids:
        if hass.states.get(e) is not None:
            resolved_ids.append(e)
            continue
        rid, candidates = _resolve_entity_id(hass, e, domain_hint=domain)
        if rid is None:
            return {
                "error": f"unknown entity_id '{e}'. Pick the exact entity_id from "
                "the candidates below (or call list_states) and retry.",
                "candidates": candidates,
            }
        resolved_ids.append(rid)
        if rid != e:
            corrections[e] = rid
    if ids:
        data["entity_id"] = resolved_ids if isinstance(raw, (list, tuple)) else resolved_ids[0]
        ids = resolved_ids

    await hass.services.async_call(domain, service, data, blocking=True)
    # Let derived entities (e.g. template lights) re-render from their source
    # before we read back the resulting state, so feedback isn't stale.
    await asyncio.sleep(0.2)
    result: dict[str, Any] = {"ok": True, "called": f"{domain}.{service}", "data": data}
    if corrections:
        result["resolved"] = corrections
    if ids:
        result["states"] = {
            e: (s.state if (s := hass.states.get(e)) else None) for e in ids
        }
    return result


async def _read_file(hass: HomeAssistant, path: str) -> dict[str, Any]:
    target = _safe_path(hass, path)
    if not os.path.isfile(target):
        return {"error": f"file '{path}' not found"}

    def _read() -> str:
        with open(target, encoding="utf-8") as f:
            return f.read()

    content = await hass.async_add_executor_job(_read)
    return {"path": path, "content": content}


async def _write_file(hass: HomeAssistant, store: dict, path: str, content: str) -> dict[str, Any]:
    if not store.get(CONF_ALLOW_WRITE, True):
        return {"error": "writes are disabled (allow_write: false)"}
    target = _safe_path(hass, path)

    def _write() -> None:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.isfile(target):
            with open(target, encoding="utf-8") as f:
                backup = f.read()
            with open(target + ".copilot.bak", "w", encoding="utf-8") as f:
                f.write(backup)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

    await hass.async_add_executor_job(_write)
    return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}


async def _check_config(hass: HomeAssistant) -> dict[str, Any]:
    res = await async_check_ha_config_file(hass)
    if res.errors:
        return {"valid": False, "errors": [e.message for e in res.errors]}
    return {"valid": True, "warnings": [w.message for w in res.warnings]}


async def _create_automation(hass: HomeAssistant, automation: dict) -> dict[str, Any]:
    """Append an automation to automations.yaml and reload."""
    path = _safe_path(hass, "automations.yaml")

    def _append() -> int:
        existing: list = []
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, list):
                    existing = loaded
        if "id" not in automation:
            automation["id"] = f"copilot_{len(existing) + 1}_{abs(hash(str(automation))) % 100000}"
        existing.append(automation)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)
        return len(existing)

    total = await hass.async_add_executor_job(_append)
    if hass.services.has_service("automation", "reload"):
        await hass.services.async_call("automation", "reload", {}, blocking=True)
    return {"ok": True, "automation_id": automation.get("id"), "total_automations": total}


async def _reload(hass: HomeAssistant, domain: str) -> dict[str, Any]:
    if domain in ("core", "homeassistant"):
        await hass.services.async_call("homeassistant", "reload_all", {}, blocking=True)
        return {"ok": True, "reloaded": "all"}
    if not hass.services.has_service(domain, "reload"):
        return {"error": f"'{domain}' has no reload service"}
    await hass.services.async_call(domain, "reload", {}, blocking=True)
    return {"ok": True, "reloaded": domain}


async def _restart(hass: HomeAssistant, store: dict) -> dict[str, Any]:
    if not store.get(CONF_ALLOW_RESTART, False):
        return {"error": "restart is disabled (set allow_restart: true to enable)"}
    await hass.services.async_call("homeassistant", "restart", {}, blocking=False)
    return {"ok": True, "restarting": True}


async def _list_areas(hass: HomeAssistant) -> dict[str, Any]:
    reg = ar.async_get(hass)
    return {"areas": [{"id": a.id, "name": a.name} for a in reg.async_list_areas()]}


async def _registry_overview(hass: HomeAssistant) -> dict[str, Any]:
    ent = er.async_get(hass)
    dev = dr.async_get(hass)
    area = ar.async_get(hass)
    return {
        "entities": len(ent.entities),
        "devices": len(dev.devices),
        "areas": len(list(area.async_list_areas())),
    }


async def _read_logs(hass: HomeAssistant, lines: int = 60) -> dict[str, Any]:
    target = _safe_path(hass, "home-assistant.log")
    if not os.path.isfile(target):
        return {"error": "log file not found"}

    def _tail() -> str:
        with open(target, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-int(lines):])

    return {"log_tail": await hass.async_add_executor_job(_tail)}


async def _create_area(hass: HomeAssistant, name: str) -> dict[str, Any]:
    """Create an area (room/zone) in the area registry, idempotently."""
    reg = ar.async_get(hass)
    existing = next((a for a in reg.async_list_areas() if a.name == name), None)
    if existing is not None:
        return {"ok": True, "area_id": existing.id, "name": existing.name, "existed": True}
    area = reg.async_create(name)
    return {"ok": True, "area_id": area.id, "name": area.name}


def _resolve_area_id(hass: HomeAssistant, area: str) -> str | None:
    reg = ar.async_get(hass)
    if reg.async_get_area(area) is not None:
        return area
    match = next((a for a in reg.async_list_areas() if a.name == area), None)
    return match.id if match else None


async def _update_entity(
    hass: HomeAssistant,
    entity_id: str,
    *,
    name: str | None = None,
    area: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Update an entity registry entry (rename / assign area / enable-disable)."""
    reg = er.async_get(hass)
    if reg.async_get(entity_id) is None:
        return {
            "error": f"entity '{entity_id}' is not in the entity registry. "
            "Only registry-backed entities (those with a unique_id) can be "
            "renamed/assigned/disabled here.",
        }
    kwargs: dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    if area is not None:
        area_id = _resolve_area_id(hass, area)
        if area_id is None:
            return {"error": f"area '{area}' not found; create it first with create_area"}
        kwargs["area_id"] = area_id
    if enabled is not None:
        kwargs["disabled_by"] = None if enabled else er.RegistryEntryDisabler.USER
    if not kwargs:
        return {"error": "nothing to update (provide name, area, or enabled)"}
    updated = reg.async_update_entity(entity_id, **kwargs)
    return {
        "ok": True,
        "entity_id": updated.entity_id,
        "name": updated.name,
        "area_id": updated.area_id,
        "disabled": updated.disabled_by is not None,
    }


async def _render_template(hass: HomeAssistant, template: str) -> dict[str, Any]:
    """Render a Jinja2 template against live HA state (Developer Tools > Template)."""
    try:
        result = Template(template, hass).async_render()
    except Exception as err:  # noqa: BLE001 - template errors are user-facing
        return {"error": f"template error: {type(err).__name__}: {err}"}
    return {"ok": True, "result": result}


async def _get_history(hass: HomeAssistant, entity_id: str, hours: int = 24) -> dict[str, Any]:
    """Return recorded state changes for an entity over the last N hours."""
    if "recorder" not in hass.config.components:
        return {"error": "the recorder integration is not enabled, so no history is available"}
    if hass.states.get(entity_id) is None:
        resolved, candidates = _resolve_entity_id(hass, entity_id)
        if resolved is None:
            return {"error": f"entity '{entity_id}' not found", "candidates": candidates}
        entity_id = resolved
    start = dt_util.utcnow() - timedelta(hours=int(hours))

    def _query() -> dict:
        return _recorder_history.state_changes_during_period(hass, start, None, entity_id)

    data = await _recorder_get_instance(hass).async_add_executor_job(_query)
    series = [
        {"state": s.state, "when": s.last_changed.isoformat()}
        for s in data.get(entity_id, [])
    ]
    return {"entity_id": entity_id, "count": len(series), "history": series[-100:]}


async def _create_scene(hass: HomeAssistant, name: str, entities: dict) -> dict[str, Any]:
    """Append a scene to scenes.yaml and reload."""
    path = _safe_path(hass, "scenes.yaml")

    def _append() -> int:
        existing: list = []
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, list):
                    existing = loaded
        scene = {"id": f"copilot_scene_{len(existing) + 1}", "name": name, "entities": entities}
        existing.append(scene)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)
        return len(existing)

    total = await hass.async_add_executor_job(_append)
    if hass.services.has_service("scene", "reload"):
        await hass.services.async_call("scene", "reload", {}, blocking=True)
    return {"ok": True, "name": name, "total_scenes": total}


async def _create_script(hass: HomeAssistant, alias: str, sequence: Any) -> dict[str, Any]:
    """Append a script to scripts.yaml (keyed by a slug) and reload."""
    path = _safe_path(hass, "scripts.yaml")
    if isinstance(sequence, dict):
        sequence = [sequence]
    slug = re.sub(r"[^a-z0-9_]+", "_", alias.lower()).strip("_") or "copilot_script"

    def _append() -> str:
        existing: dict = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    existing = loaded
        key = slug
        i = 2
        while key in existing:
            key = f"{slug}_{i}"
            i += 1
        existing[key] = {"alias": alias, "sequence": sequence}
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)
        return key

    key = await hass.async_add_executor_job(_append)
    if hass.services.has_service("script", "reload"):
        await hass.services.async_call("script", "reload", {}, blocking=True)
    return {"ok": True, "script_entity_id": f"script.{key}"}


async def dispatch(hass: HomeAssistant, store: dict, name: str, args: dict) -> dict[str, Any]:
    """Execute a tool by name with the given arguments."""
    try:
        if name == "list_states":
            return await _list_states(hass, args.get("domain"))
        if name == "get_state":
            return await _get_state(hass, args["entity_id"])
        if name == "list_services":
            return await _list_services(hass, args.get("domain"))
        if name == "call_service":
            # Tolerate entity_id / brightness / etc. passed either nested under
            # "data" or flattened at the top level (smaller models often do the
            # latter); merge everything that isn't a reserved key into data.
            data: dict[str, Any] = dict(args.get("data") or {})
            for key, val in args.items():
                if key not in ("domain", "service", "data") and key not in data:
                    data[key] = val
            return await _call_service(hass, args["domain"], args["service"], data)
        if name == "read_config_file":
            return await _read_file(hass, args["path"])
        if name == "write_config_file":
            return await _write_file(hass, store, args["path"], args["content"])
        if name == "check_config":
            return await _check_config(hass)
        if name == "create_automation":
            # Accept either {"automation": {...}} or the automation fields
            # (alias/trigger/action/...) passed directly at the top level.
            auto = args.get("automation")
            if not isinstance(auto, dict):
                auto = {k: v for k, v in args.items() if k != "automation"}
            return await _create_automation(hass, auto)
        if name == "reload":
            return await _reload(hass, args["domain"])
        if name == "restart":
            return await _restart(hass, store)
        if name == "list_areas":
            return await _list_areas(hass)
        if name == "registry_overview":
            return await _registry_overview(hass)
        if name == "read_logs":
            return await _read_logs(hass, args.get("lines", 60))
        if name == "create_area":
            return await _create_area(hass, args["name"])
        if name == "rename_entity":
            return await _update_entity(hass, args["entity_id"], name=args["name"])
        if name == "assign_entity_area":
            return await _update_entity(hass, args["entity_id"], area=args["area"])
        if name == "set_entity_enabled":
            return await _update_entity(hass, args["entity_id"], enabled=bool(args["enabled"]))
        if name == "render_template":
            return await _render_template(hass, args["template"])
        if name == "get_history":
            return await _get_history(hass, args["entity_id"], args.get("hours", 24))
        if name == "create_scene":
            return await _create_scene(hass, args["name"], args.get("entities") or {})
        if name == "create_script":
            seq = args.get("sequence")
            if seq is None:
                seq = args.get("action")
            return await _create_script(hass, args["alias"], seq)
        return {"error": f"unknown tool '{name}'"}
    except KeyError as err:
        return {"error": f"missing required argument: {err}"}
    except Exception as err:  # noqa: BLE001 - surface any tool failure to the agent
        return {"error": f"{type(err).__name__}: {err}"}


# OpenAI-style function specifications advertised to the model.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_states",
            "description": "List entities and their current state. Optionally filter by domain (e.g. 'light', 'switch', 'sensor').",
            "parameters": {
                "type": "object",
                "properties": {"domain": {"type": "string", "description": "Optional domain filter"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "Get the full state and attributes of one entity.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_services",
            "description": "List callable services. Optionally filter by domain.",
            "parameters": {
                "type": "object",
                "properties": {"domain": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_service",
            "description": "Call any Home Assistant service, e.g. domain='light', service='turn_on', entity_id='light.living_room'. Pass the target as 'entity_id' (full id including the domain prefix). Extra parameters like brightness can go in 'data'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Service domain, e.g. 'light'"},
                    "service": {"type": "string", "description": "Service name, e.g. 'turn_on'"},
                    "entity_id": {
                        "type": "string",
                        "description": "Target entity_id, e.g. 'light.living_room'. Use the exact full id from list_states.",
                    },
                    "data": {"type": "object", "description": "Optional extra service params, e.g. {brightness: 255}"},
                },
                "required": ["domain", "service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_config_file",
            "description": "Read a text file inside the HA config directory (e.g. 'configuration.yaml', 'automations.yaml').",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_config_file",
            "description": "Overwrite a text file inside the HA config directory. A .copilot.bak backup is kept. Always run check_config afterwards.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_config",
            "description": "Validate the Home Assistant configuration. Use before reloading/restarting after edits.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_automation",
            "description": "Create a new automation by appending it to automations.yaml and reloading. Provide the automation fields directly: alias (string), trigger (object or list), action (list), and optional condition.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string", "description": "Human-readable name"},
                    "trigger": {"description": "Trigger object or list, e.g. {platform: sun, event: sunset}"},
                    "condition": {"description": "Optional condition(s)"},
                    "action": {"type": "array", "description": "List of action steps, e.g. [{service: light.turn_on, data: {entity_id: light.living_room}}]"},
                },
                "required": ["alias", "trigger", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload",
            "description": "Reload a domain's YAML config without restarting (e.g. 'automation', 'script', 'template', or 'core' for reload_all).",
            "parameters": {
                "type": "object",
                "properties": {"domain": {"type": "string"}},
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart",
            "description": "Restart Home Assistant (disabled unless allow_restart is true).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_areas",
            "description": "List all areas (rooms/zones) defined in Home Assistant.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "registry_overview",
            "description": "Counts of entities, devices and areas registered in HA.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_logs",
            "description": "Read the tail of the Home Assistant log to diagnose problems.",
            "parameters": {
                "type": "object",
                "properties": {"lines": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_area",
            "description": "Create an area (room/zone), e.g. '客厅', '卧室'. Idempotent: returns the existing area if the name already exists.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Area name"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_entity",
            "description": "Set the friendly display name of an entity in the entity registry (like renaming it in Settings UI). Only works for registry-backed entities.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "name": {"type": "string", "description": "New display name"},
                },
                "required": ["entity_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_entity_area",
            "description": "Assign an entity to an area (by area name or area_id). Create the area first with create_area if it does not exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "area": {"type": "string", "description": "Area name or area_id"},
                },
                "required": ["entity_id", "area"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_entity_enabled",
            "description": "Enable or disable an entity in the registry (disabled entities stop updating). Only registry-backed entities.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["entity_id", "enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_template",
            "description": "Render a Jinja2 template against live HA state, e.g. \"{{ states('sensor.x') }}\" or \"{{ states.light | selectattr('state','eq','on') | list | count }}\". Use this to compute/inspect state.",
            "parameters": {
                "type": "object",
                "properties": {"template": {"type": "string"}},
                "required": ["template"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_history",
            "description": "Get recorded state changes for an entity over the last N hours (default 24).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "hours": {"type": "integer", "description": "Look-back window in hours"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_scene",
            "description": "Create a scene by appending to scenes.yaml and reloading. Provide name and entities as a mapping of entity_id -> desired state, e.g. {\"light.living_room\": \"on\", \"light.bedroom\": \"off\"}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "entities": {"type": "object", "description": "entity_id -> state mapping"},
                },
                "required": ["name", "entities"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_script",
            "description": "Create a script by appending to scripts.yaml and reloading. Provide alias and sequence (a list of action steps, e.g. [{service: light.turn_on, data: {entity_id: light.living_room}}]).",
            "parameters": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string"},
                    "sequence": {"type": "array", "description": "List of action steps"},
                },
                "required": ["alias", "sequence"],
            },
        },
    },
]

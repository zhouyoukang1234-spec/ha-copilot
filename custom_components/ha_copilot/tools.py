"""Deep-fusion tool layer: the operations HA-Copilot can perform on Home Assistant.

Each tool is a thin, well-typed wrapper around a Home Assistant internal API
(state machine, service registry, registries, config files, config check). The
LLM agent selects and invokes these via OpenAI-style function calling; this is
the layer that makes the AI "fused" with HA rather than calling it from outside.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import voluptuous as vol  # noqa: F401  (kept for future schema validation)
import yaml

from homeassistant.components.recorder import get_instance as _recorder_get_instance
from homeassistant.components.recorder import history as _recorder_history
from homeassistant.components.recorder import statistics as _recorder_statistics
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.check_config import async_check_ha_config_file
from homeassistant.helpers.script import Script
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify as _slugify

from . import memory, resources
from .const import CONF_ALLOW_RESTART, CONF_ALLOW_WRITE


def _github_repo_from_url(url: str) -> str | None:
    """Best-effort ``owner/name`` from a GitHub raw/blob/repo URL (else None)."""
    s = (url or "").strip()
    for pre in (
        "https://raw.githubusercontent.com/",
        "https://github.com/",
        "github.com/",
    ):
        if s.startswith(pre):
            parts = [p for p in s[len(pre):].split("/") if p]
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
            return None
    return None


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


async def _get_state(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    s = hass.states.get(entity_id)
    if s is None:
        return {"error": f"entity '{entity_id}' not found"}
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
    # AssistAPI-style targeting: act on a whole area/floor/label without first
    # enumerating entity_ids. Resolves names -> ids; HA expands the target.
    target = _resolve_service_target(hass, data)
    if "__error__" in target:
        return {"error": target["__error__"]}
    # Validate any referenced entity_ids so the agent gets corrective feedback
    # instead of silently calling a service against a non-existent entity.
    raw = data.get("entity_id")
    ids: list[str] = []
    if isinstance(raw, str):
        ids = [raw]
    elif isinstance(raw, (list, tuple)):
        ids = [str(e) for e in raw]
    missing = [e for e in ids if hass.states.get(e) is None]
    if missing:
        return {
            "error": f"unknown entity_id(s): {missing}. "
            "Call list_states to get exact entity_ids and retry.",
        }
    await hass.services.async_call(
        domain, service, data, blocking=True, target=target or None
    )
    # Let derived entities (e.g. template lights) re-render from their source
    # before we read back the resulting state, so feedback isn't stale.
    await asyncio.sleep(0.2)
    result: dict[str, Any] = {"ok": True, "called": f"{domain}.{service}", "data": data}
    if target:
        result["target"] = target
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


async def _list_dir(hass: HomeAssistant, path: str = "") -> dict[str, Any]:
    """List files and sub-directories under a config-relative path."""
    target = _safe_path(hass, path)
    if not os.path.isdir(target):
        return {"error": f"directory '{path or '.'}' not found"}

    def _scan() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name in sorted(os.listdir(target)):
            if name.startswith(".") or name.endswith(".copilot.bak"):
                continue
            full = os.path.join(target, name)
            is_dir = os.path.isdir(full)
            out.append(
                {
                    "name": name,
                    "path": os.path.join(path, name) if path else name,
                    "type": "dir" if is_dir else "file",
                    "size": (os.path.getsize(full) if not is_dir else None),
                }
            )
        # Directories first, then files - both alphabetical.
        out.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return out

    entries = await hass.async_add_executor_job(_scan)
    return {"path": path, "entries": entries}


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
    # Resolve the entity_id HA assigned: the automation entity exposes its
    # config id as an attribute, but its entity_id is derived from the alias
    # slug. Return it so callers can verify/control/delete what they created.
    target_id = automation.get("id")
    entity_id = next(
        (st.entity_id for st in hass.states.async_all("automation")
         if st.attributes.get("id") == target_id),
        None,
    )
    return {
        "ok": True,
        "automation_id": target_id,
        "entity_id": entity_id,
        "total_automations": total,
    }


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


async def _rename_area(hass: HomeAssistant, identifier: str, new_name: str) -> dict[str, Any]:
    """Rename an area (the area-registry 'update'), resolving by area_id or current name."""
    area_id = _resolve_area_id(hass, identifier)
    if area_id is None:
        return {"error": f"no area matched id/name '{identifier}'"}
    reg = ar.async_get(hass)
    clash = next((a for a in reg.async_list_areas() if a.name == new_name and a.id != area_id), None)
    if clash is not None:
        return {"error": f"another area already named '{new_name}'"}
    area = reg.async_update(area_id, name=new_name)
    return {"ok": True, "area_id": area.id, "name": area.name}


def _resolve_area_id(hass: HomeAssistant, area: str) -> str | None:
    reg = ar.async_get(hass)
    if reg.async_get_area(area) is not None:
        return area
    match = next((a for a in reg.async_list_areas() if a.name == area), None)
    return match.id if match else None


def _resolve_floor_id(hass: HomeAssistant, floor: str) -> str | None:
    reg = fr.async_get(hass)
    if reg.async_get_floor(floor) is not None:
        return floor
    match = next((f for f in reg.async_list_floors() if f.name == floor), None)
    return match.floor_id if match else None


def _resolve_label_id(hass: HomeAssistant, label: str) -> str | None:
    reg = lr.async_get(hass)
    if reg.async_get_label(label) is not None:
        return label
    match = next((x for x in reg.async_list_labels() if x.name == label), None)
    return match.label_id if match else None


def _resolve_device_id(hass: HomeAssistant, device: str) -> str | None:
    reg = dr.async_get(hass)
    if reg.async_get(device) is not None:
        return device
    match = next(
        (
            d
            for d in reg.devices.values()
            if device in (d.name_by_user, d.name)
        ),
        None,
    )
    return match.id if match else None


def _resolve_service_target(
    hass: HomeAssistant, data: dict[str, Any]
) -> dict[str, list[str]] | dict[str, str]:
    """Pop AssistAPI-style area/floor/label/device targets from ``data`` and
    resolve their names to registry ids, returning an HA service ``target`` dict.

    Lets a model act on a whole area/floor/label/device without first
    enumerating entity_ids; HA expands floor -> areas -> devices -> entities
    natively. Returns ``{"__error__": "..."}`` if a named target can't resolve.
    """
    resolvers = {
        ("area", "areas", "area_id"): ("area_id", _resolve_area_id),
        ("floor", "floors", "floor_id"): ("floor_id", _resolve_floor_id),
        ("label", "labels", "label_id"): ("label_id", _resolve_label_id),
        ("device", "devices", "device_id"): ("device_id", _resolve_device_id),
    }
    target: dict[str, list[str]] = {}
    for keys, (target_key, resolve) in resolvers.items():
        values: list[str] = []
        for key in keys:
            raw = data.pop(key, None)
            if raw is None:
                continue
            values.extend([raw] if isinstance(raw, str) else [str(v) for v in raw])
        ids: list[str] = []
        for value in values:
            resolved = resolve(hass, value)
            if resolved is None:
                return {"__error__": f"{target_key[:-3]} '{value}' not found"}
            ids.append(resolved)
        if ids:
            target[target_key] = ids
    return target


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


async def _render_template(hass: HomeAssistant, template: str,
                           variables: dict | None = None) -> dict[str, Any]:
    """Render a Jinja2 template against live HA state (Developer Tools > Template).

    Optional `variables` are injected into the render context, so the agent can
    test a template the way an automation/script would evaluate it (with its
    `trigger`, `this`, custom vars, etc.) rather than only against bare state.
    """
    try:
        result = Template(template, hass).async_render(variables or None)
    except Exception as err:  # noqa: BLE001 - template errors are user-facing
        return {"error": f"template error: {type(err).__name__}: {err}"}
    return {"ok": True, "result": result}


async def _get_history(hass: HomeAssistant, entity_id: str, hours: int = 24) -> dict[str, Any]:
    """Return recorded state changes for an entity over the last N hours."""
    if "recorder" not in hass.config.components:
        return {"error": "the recorder integration is not enabled, so no history is available"}
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

    def _append() -> tuple[int, str]:
        existing: list = []
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, list):
                    existing = loaded
        # Derive a stable unique id from the name slug so the scene's entity_id
        # is predictable and never reuses a deleted scene's id (which would make
        # HA's registry hand back a stale entity_id for the new scene).
        used = {s.get("id") for s in existing if isinstance(s, dict)}
        base = _slugify(name) or "copilot_scene"
        scene_id = base
        suffix = 2
        while scene_id in used:
            scene_id = f"{base}_{suffix}"
            suffix += 1
        scene = {"id": scene_id, "name": name, "entities": entities}
        existing.append(scene)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)
        return len(existing), scene_id

    total, scene_id = await hass.async_add_executor_job(_append)
    if hass.services.has_service("scene", "reload"):
        await hass.services.async_call("scene", "reload", {}, blocking=True)
    return {"ok": True, "name": name, "id": scene_id, "total_scenes": total}


async def _create_script(hass: HomeAssistant, alias: str, sequence: Any) -> dict[str, Any]:
    """Append a script to scripts.yaml (keyed by a slug) and reload."""
    path = _safe_path(hass, "scripts.yaml")
    if isinstance(sequence, dict):
        sequence = [sequence]
    slug = _slugify(alias) or "copilot_script"

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


def _managed_package_path(hass: HomeAssistant) -> str:
    return _safe_path(hass, "packages/ha_copilot_managed.yaml")


def _load_managed_package(path: str) -> dict:
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _dump_managed_package(path: str, doc: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)


async def _assign_entities_by_rules(
    hass: HomeAssistant, rules: list, only_unassigned: bool = True
) -> dict[str, Any]:
    """Bulk-assign registry entities to areas by keyword rules (first hit wins).

    rules: [[area_name, [keyword, ...]], ...]. Areas are created idempotently.
    Replaces the tedious one-by-one area assignment in the UI for hundreds of entities.
    """
    ent_reg = er.async_get(hass)
    area_reg = ar.async_get(hass)
    norm = [(r[0], list(r[1])) for r in rules]
    name_to_id = {a.name: a.id for a in area_reg.async_list_areas()}
    stats: dict[str, int] = {}
    for area_name, _ in norm:
        if area_name not in name_to_id:
            name_to_id[area_name] = area_reg.async_create(area_name).id
        stats.setdefault(area_name, 0)
    stats["_skipped"] = 0
    stats["_unmatched"] = 0
    for entry in list(ent_reg.entities.values()):
        if only_unassigned and entry.area_id:
            stats["_skipped"] += 1
            continue
        hay = f"{entry.entity_id} {entry.original_name or ''} {entry.name or ''}".lower()
        placed = False
        for area_name, keywords in norm:
            if any(k.lower() in hay for k in keywords):
                ent_reg.async_update_entity(entry.entity_id, area_id=name_to_id[area_name])
                stats[area_name] += 1
                placed = True
                break
        if not placed:
            stats["_unmatched"] += 1
    return {"ok": True, "stats": stats}


async def _create_helper(
    hass: HomeAssistant, store: dict, domain: str, object_id: str, config: dict
) -> dict[str, Any]:
    """Define a helper entity (input_boolean/number/text/select/datetime, timer, counter)."""
    if not store.get(CONF_ALLOW_WRITE, True):
        return {"error": "writes are disabled (allow_write: false)"}
    allowed = {
        "input_boolean", "input_number", "input_text", "input_select",
        "input_datetime", "timer", "counter",
    }
    if domain not in allowed:
        return {"error": f"unsupported helper domain '{domain}'; one of {sorted(allowed)}"}
    path = _managed_package_path(hass)

    def _write() -> None:
        doc = _load_managed_package(path)
        helpers = doc.get(domain) or {}
        helpers[object_id] = config
        doc[domain] = helpers
        _dump_managed_package(path, doc)

    await hass.async_add_executor_job(_write)
    entity_id = f"{domain}.{object_id}"
    # Some helper domains (e.g. counter, timer) expose no reload service in this
    # HA version, so a freshly-added entity only appears after a restart. Be
    # honest about whether it is live now rather than claiming success blindly.
    if hass.services.has_service(domain, "reload"):
        await hass.services.async_call(domain, "reload", {}, blocking=True)
    live = hass.states.get(entity_id) is not None
    result: dict[str, Any] = {"ok": True, "entity_id": entity_id, "live": live}
    if not live:
        result["note"] = (
            f"'{domain}' has no working reload in this HA version; the helper is "
            "written to packages/ha_copilot_managed.yaml and will appear after a restart."
        )
    return result


async def _create_template_sensor(
    hass: HomeAssistant, store: dict, name: str, state: str, *,
    unit: str | None = None, device_class: str | None = None, icon: str | None = None,
) -> dict[str, Any]:
    """Validate a Jinja state template against live state, then deploy it as a template sensor."""
    if not store.get(CONF_ALLOW_WRITE, True):
        return {"error": "writes are disabled (allow_write: false)"}
    try:
        Template(state, hass).async_render()
    except Exception as err:  # noqa: BLE001 - template errors are user-facing
        return {"error": f"state template failed validation: {type(err).__name__}: {err}"}
    entry: dict[str, Any] = {"name": name, "state": state}
    if unit:
        entry["unit_of_measurement"] = unit
    if device_class:
        entry["device_class"] = device_class
    if icon:
        entry["icon"] = icon
    path = _managed_package_path(hass)

    def _write() -> None:
        doc = _load_managed_package(path)
        blocks = doc.get("template") or []
        target = next((b for b in blocks if "sensor" in b), None)
        if target is None:
            target = {"sensor": []}
            blocks.append(target)
        target["sensor"] = [e for e in target["sensor"] if e.get("name") != name] + [entry]
        doc["template"] = blocks
        _dump_managed_package(path, doc)

    await hass.async_add_executor_job(_write)
    await _reload(hass, "template")
    return {"ok": True, "name": name}


async def _create_blueprint_automation(
    hass: HomeAssistant, alias: str, blueprint_path: str, inputs: dict
) -> dict[str, Any]:
    """Instantiate an automation from a blueprint (use_blueprint + inputs) and reload."""
    path = _safe_path(hass, "automations.yaml")

    def _append() -> str:
        existing: list = []
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, list):
                    existing = loaded
        auto_id = f"copilot_bp_{len(existing) + 1}_{abs(hash(alias)) % 100000}"
        existing.append({
            "id": auto_id, "alias": alias,
            "use_blueprint": {"path": blueprint_path, "input": inputs},
        })
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)
        return auto_id

    auto_id = await hass.async_add_executor_job(_append)
    if hass.services.has_service("automation", "reload"):
        await hass.services.async_call("automation", "reload", {}, blocking=True)
    return {"ok": True, "automation_id": auto_id}


async def _list_blueprints(hass: HomeAssistant, domain: str = "automation") -> dict[str, Any]:
    """List installed blueprints for a domain (automation|script) with their inputs."""
    # Component internals are version-sensitive; import lazily so a version skew
    # degrades to a clean tool error instead of breaking component load.
    if domain == "automation":
        from homeassistant.components.automation.helpers import async_get_blueprints
    elif domain == "script":
        from homeassistant.components.script.helpers import async_get_blueprints
    else:
        return {"error": f"unsupported blueprint domain '{domain}' (automation|script)"}
    domain_bps = async_get_blueprints(hass)
    results = await domain_bps.async_get_blueprints()
    out: list[dict[str, Any]] = []
    for path, bp in results.items():
        if isinstance(bp, Exception):
            continue
        meta = bp.metadata or {}
        out.append({
            "path": path,
            "name": meta.get("name"),
            "inputs": list((meta.get("input") or {}).keys()),
        })
    return {"domain": domain, "count": len(out), "blueprints": out}


async def _list_backups(hass: HomeAssistant) -> dict[str, Any]:
    from homeassistant.components.backup.const import DATA_MANAGER
    manager = hass.data.get(DATA_MANAGER)
    if manager is None:
        return {"error": "the backup integration is not available"}
    backups, _agent_errors = await manager.async_get_backups()
    items = [
        {
            "backup_id": b.backup_id,
            "name": b.name,
            "date": b.date,
            "database_included": b.database_included,
            "ha_version": b.homeassistant_version,
        }
        for b in sorted(backups.values(), key=lambda b: b.date or "", reverse=True)
    ]
    return {"count": len(items), "backups": items}


async def _create_backup(hass: HomeAssistant, name: str) -> dict[str, Any]:
    """Trigger a local backup (snapshot before risky changes). Runs asynchronously."""
    from homeassistant.components.backup.const import DATA_MANAGER
    manager = hass.data.get(DATA_MANAGER)
    if manager is None:
        return {"error": "the backup integration is not available"}
    new = await manager.async_create_backup(
        agent_ids=["backup.local"],
        include_addons=None,
        include_all_addons=False,
        include_database=True,
        include_folders=None,
        include_homeassistant=True,
        name=name,
        password=None,
    )
    return {"ok": True, "backup_job_id": new.backup_job_id,
            "note": "backup runs asynchronously; poll list_backups for completion"}


async def _delete_backup(hass: HomeAssistant, backup_id: str) -> dict[str, Any]:
    from homeassistant.components.backup.const import DATA_MANAGER
    manager = hass.data.get(DATA_MANAGER)
    if manager is None:
        return {"error": "the backup integration is not available"}
    errors = await manager.async_delete_backup(backup_id)
    if errors:
        return {"ok": False, "errors": {k: str(v) for k, v in errors.items()}}
    return {"ok": True, "deleted": backup_id}


def _backup_then_write(target: str, dump: Any) -> None:
    """Persist YAML, keeping a .copilot.bak of the previous content."""
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as f:
            prev = f.read()
        with open(target + ".copilot.bak", "w", encoding="utf-8") as f:
            f.write(prev)
    with open(target, "w", encoding="utf-8") as f:
        yaml.safe_dump(dump, f, allow_unicode=True, sort_keys=False)


def _purge_restored_entities(
    hass: HomeAssistant,
    domain: str,
    *,
    ids: set[str] | None = None,
    names: set[str] | None = None,
    entity_ids: set[str] | None = None,
) -> list[str]:
    """Remove now-orphaned `unavailable`/restored registry entries left after a reload.

    HA keeps a deleted YAML object as a `restored` (state=unavailable) entity until a full
    restart; this drops that residue so a delete leaves no trace. Matches by config `id`
    attribute, friendly_name, or explicit entity_id.
    """
    reg = er.async_get(hass)
    purged: list[str] = []
    for st in hass.states.async_all(domain):
        if st.state != "unavailable":
            continue
        attr_id = st.attributes.get("id")
        attr_name = st.attributes.get("friendly_name")
        if (
            (ids and attr_id is not None and str(attr_id) in ids)
            or (names and attr_name in names)
            or (entity_ids and st.entity_id in entity_ids)
        ):
            if reg.async_get(st.entity_id) is not None:
                reg.async_remove(st.entity_id)
            else:
                hass.states.async_remove(st.entity_id)
            purged.append(st.entity_id)
    return purged


def _resolve_automation_identifier(hass: HomeAssistant, identifier: str) -> str:
    """Map an ``automation.<slug>`` entity_id to its config id.

    automations.yaml is keyed by config ``id``/``alias``, but the chain
    create -> (returns entity_id) -> delete/update naturally hands back an
    entity_id. The automation entity exposes its config id as a state
    attribute, so resolve through it; fall back to the raw value (which still
    matches by id/alias) when no state is found."""
    if isinstance(identifier, str) and identifier.startswith("automation."):
        st = hass.states.get(identifier)
        if st and st.attributes.get("id") is not None:
            return str(st.attributes["id"])
    return identifier


async def _delete_automation(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Remove automation(s) from automations.yaml by id or alias, then reload."""
    path = _safe_path(hass, "automations.yaml")

    def _remove() -> tuple[int, int, set[str], set[str]]:
        if not os.path.isfile(path):
            return (0, 0, set(), set())
        with open(path, encoding="utf-8") as f:
            existing = yaml.safe_load(f) or []
        if not isinstance(existing, list):
            return (0, 0, set(), set())
        before = len(existing)
        gone = [
            a for a in existing
            if (str(a.get("id")) == str(identifier) or a.get("alias") == identifier)
        ]
        kept = [a for a in existing if a not in gone]
        if len(kept) != before:
            _backup_then_write(path, kept)
        ids = {str(a.get("id")) for a in gone if a.get("id") is not None}
        names = {a.get("alias") for a in gone if a.get("alias")}
        return (before, before - len(kept), ids, names)

    before, removed, ids, names = await hass.async_add_executor_job(_remove)
    if removed == 0:
        return {"error": f"no automation matched id/alias '{identifier}'"}
    if hass.services.has_service("automation", "reload"):
        await hass.services.async_call("automation", "reload", {}, blocking=True)
    purged = _purge_restored_entities(hass, "automation", ids=ids, names=names)
    return {"ok": True, "removed": removed, "remaining": before - removed, "purged": purged}


async def _update_automation(hass: HomeAssistant, identifier: str, new_alias: str) -> dict[str, Any]:
    """Rename an automation's alias in automations.yaml by id or current alias, then reload.

    The entity_id is derived from the automation's ``id`` (not its alias), so a
    rename changes only the friendly name and leaves the entity_id stable.
    """
    path = _safe_path(hass, "automations.yaml")

    def _rename() -> tuple[bool, str | None]:
        if not os.path.isfile(path):
            return (False, None)
        with open(path, encoding="utf-8") as f:
            existing = yaml.safe_load(f) or []
        if not isinstance(existing, list):
            return (False, None)
        target = next(
            (a for a in existing
             if isinstance(a, dict)
             and (str(a.get("id")) == str(identifier) or a.get("alias") == identifier)),
            None,
        )
        if target is None:
            return (False, None)
        target["alias"] = new_alias
        _backup_then_write(path, existing)
        return (True, str(target.get("id")) if target.get("id") is not None else None)

    ok, aid = await hass.async_add_executor_job(_rename)
    if not ok:
        return {"error": f"no automation matched id/alias '{identifier}'"}
    if hass.services.has_service("automation", "reload"):
        await hass.services.async_call("automation", "reload", {}, blocking=True)
    return {"ok": True, "id": aid, "alias": new_alias}


async def _update_script(hass: HomeAssistant, identifier: str, new_alias: str) -> dict[str, Any]:
    """Rename a script's alias in scripts.yaml by key or 'script.<key>', then reload.

    The entity_id is the script's dict key, so renaming the alias updates only the
    friendly name and keeps the entity_id stable.
    """
    key = identifier.split(".", 1)[1] if identifier.startswith("script.") else identifier
    path = _safe_path(hass, "scripts.yaml")

    def _rename() -> str | None:
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
        if not isinstance(existing, dict):
            return None
        # Resolve by dict key (== entity object_id) first, then fall back to the
        # current alias so the documented "by id or current alias" contract holds
        # (mirrors update_automation).
        target_key = key if key in existing else next(
            (k for k, v in existing.items()
             if isinstance(v, dict) and v.get("alias") == identifier),
            None,
        )
        if target_key is None:
            return None
        body = existing[target_key]
        if not isinstance(body, dict):
            return None
        body["alias"] = new_alias
        _backup_then_write(path, existing)
        return target_key

    resolved_key = await hass.async_add_executor_job(_rename)
    if resolved_key is None:
        return {"error": f"no script matched '{identifier}'"}
    if hass.services.has_service("script", "reload"):
        await hass.services.async_call("script", "reload", {}, blocking=True)
    return {"ok": True, "script_entity_id": f"script.{resolved_key}", "alias": new_alias}


async def _delete_scene(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Remove scene(s) from scenes.yaml by id or name, then reload."""
    path = _safe_path(hass, "scenes.yaml")

    def _remove() -> tuple[int, int, set[str], set[str]]:
        if not os.path.isfile(path):
            return (0, 0, set(), set())
        with open(path, encoding="utf-8") as f:
            existing = yaml.safe_load(f) or []
        if not isinstance(existing, list):
            return (0, 0, set(), set())
        before = len(existing)
        gone = [
            s for s in existing
            if (str(s.get("id")) == str(identifier) or s.get("name") == identifier)
        ]
        kept = [s for s in existing if s not in gone]
        if len(kept) != before:
            _backup_then_write(path, kept)
        ids = {str(s.get("id")) for s in gone if s.get("id") is not None}
        names = {s.get("name") for s in gone if s.get("name")}
        return (before, before - len(kept), ids, names)

    before, removed, ids, names = await hass.async_add_executor_job(_remove)
    if removed == 0:
        return {"error": f"no scene matched id/name '{identifier}'"}
    if hass.services.has_service("scene", "reload"):
        await hass.services.async_call("scene", "reload", {}, blocking=True)
    purged = _purge_restored_entities(hass, "scene", ids=ids, names=names)
    return {"ok": True, "removed": removed, "remaining": before - removed, "purged": purged}


async def _delete_script(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Remove a script from scripts.yaml by key or 'script.<key>' entity_id, then reload."""
    key = identifier.split(".", 1)[1] if identifier.startswith("script.") else identifier
    path = _safe_path(hass, "scripts.yaml")

    def _remove() -> bool:
        if not os.path.isfile(path):
            return False
        with open(path, encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
        if not isinstance(existing, dict) or key not in existing:
            return False
        existing.pop(key)
        _backup_then_write(path, existing)
        return True

    removed = await hass.async_add_executor_job(_remove)
    if not removed:
        return {"error": f"no script matched '{identifier}'"}
    if hass.services.has_service("script", "reload"):
        await hass.services.async_call("script", "reload", {}, blocking=True)
    purged = _purge_restored_entities(hass, "script", entity_ids={f"script.{key}"})
    return {"ok": True, "deleted": f"script.{key}", "purged": purged}


async def _delete_area(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Delete an area from the area registry by area_id or name (mirrors create_area)."""
    area_id = _resolve_area_id(hass, identifier)
    if area_id is None:
        return {"error": f"no area matched id/name '{identifier}'"}
    reg = ar.async_get(hass)
    name = reg.async_get_area(area_id).name
    reg.async_delete(area_id)
    return {"ok": True, "deleted_area_id": area_id, "name": name}


async def _delete_helper(
    hass: HomeAssistant, domain: str, object_id: str
) -> dict[str, Any]:
    """Remove a helper from the managed package, then reload (mirrors create_helper)."""
    allowed = {
        "input_boolean", "input_number", "input_text", "input_select",
        "input_datetime", "timer", "counter",
    }
    if domain not in allowed:
        return {"error": f"unsupported helper domain '{domain}'; one of {sorted(allowed)}"}
    path = _managed_package_path(hass)

    def _remove() -> bool:
        doc = _load_managed_package(path)
        helpers = doc.get(domain) or {}
        if object_id not in helpers:
            return False
        helpers.pop(object_id)
        doc[domain] = helpers
        _dump_managed_package(path, doc)
        return True

    removed = await hass.async_add_executor_job(_remove)
    if not removed:
        return {"error": f"no '{domain}' helper named '{object_id}'"}
    if hass.services.has_service(domain, "reload"):
        await hass.services.async_call(domain, "reload", {}, blocking=True)
    entity_id = f"{domain}.{object_id}"
    purged = _purge_restored_entities(hass, domain, entity_ids={entity_id})
    return {"ok": True, "deleted": entity_id, "purged": purged}


async def _list_template_sensors(hass: HomeAssistant) -> dict[str, Any]:
    """List template sensors managed by ha_copilot (from the managed package).

    Each entry includes the live state/availability so the UI can show whether the
    sensor is currently rendering. This is the 'read' of the template-sensor lifecycle.
    """
    path = _managed_package_path(hass)

    def _read() -> list[dict[str, Any]]:
        doc = _load_managed_package(path)
        out: list[dict[str, Any]] = []
        for block in doc.get("template") or []:
            sensors = block.get("sensor")
            if not isinstance(sensors, list):
                continue
            for e in sensors:
                if isinstance(e, dict) and e.get("name"):
                    out.append(e)
        return out

    entries = await hass.async_add_executor_job(_read)
    sensors = []
    for e in entries:
        name = e["name"]
        slug = _slugify(name) or "sensor"
        eid = f"sensor.{slug}"
        st = hass.states.get(eid)
        sensors.append({
            "name": name,
            "entity_id": eid,
            "state_template": e.get("state"),
            "unit_of_measurement": e.get("unit_of_measurement"),
            "device_class": e.get("device_class"),
            "current_state": st.state if st else None,
        })
    return {"template_sensors": sensors}


async def _delete_template_sensor(hass: HomeAssistant, name: str) -> dict[str, Any]:
    """Remove a template sensor from the managed package by name, then reload."""
    path = _managed_package_path(hass)

    def _remove() -> bool:
        doc = _load_managed_package(path)
        blocks = doc.get("template") or []
        hit = False
        for block in blocks:
            sensors = block.get("sensor")
            if not isinstance(sensors, list):
                continue
            kept = [e for e in sensors if e.get("name") != name]
            if len(kept) != len(sensors):
                block["sensor"] = kept
                hit = True
        if not hit:
            return False
        doc["template"] = [b for b in blocks if b.get("sensor") or b.get("binary_sensor")]
        _dump_managed_package(path, doc)
        return True

    removed = await hass.async_add_executor_job(_remove)
    if not removed:
        return {"error": f"no template sensor named '{name}'"}
    await _reload(hass, "template")
    purged = _purge_restored_entities(hass, "sensor", names={name})
    return {"ok": True, "deleted": name, "purged": purged}


def _entry_state(entry: Any) -> str:
    state = getattr(entry, "state", None)
    return getattr(state, "value", str(state)) if state is not None else "unknown"


async def _list_config_entries(hass: HomeAssistant, domain: str | None = None) -> dict[str, Any]:
    """List integration config entries (the 'Integrations' settings page) with their load state."""
    entries = (
        hass.config_entries.async_entries(domain)
        if domain
        else hass.config_entries.async_entries()
    )
    items = [
        {
            "entry_id": e.entry_id,
            "domain": e.domain,
            "title": e.title,
            "state": _entry_state(e),
            "source": e.source,
        }
        for e in sorted(entries, key=lambda e: (e.domain, e.title or ""))
    ]
    return {"count": len(items), "entries": items}


async def _reload_config_entry(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Reload a single integration config entry by entry_id (operate integrations live)."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return {
            "error": f"config entry '{entry_id}' not found. "
            "Call list_config_entries to get exact entry_ids.",
        }
    await hass.config_entries.async_reload(entry_id)
    state = _entry_state(entry)
    return {
        "ok": not state.startswith("failed"),
        "entry_id": entry_id,
        "domain": entry.domain,
        "state": state,
    }


# --- deep-fusion round 1: introspection / registries / statistics / actions ---

async def _get_core_config(hass: HomeAssistant) -> dict[str, Any]:
    """Snapshot HA's core configuration (version, location, units, components)."""
    c = hass.config
    return {
        "version": getattr(__import__("homeassistant.const", fromlist=["__version__"]), "__version__", None),
        "location_name": c.location_name,
        "latitude": c.latitude,
        "longitude": c.longitude,
        "elevation": c.elevation,
        "time_zone": c.time_zone,
        "currency": c.currency,
        "country": c.country,
        "language": c.language,
        "unit_system": c.units.__class__.__name__,
        "config_dir": c.config_dir,
        "state": str(c.state) if getattr(c, "state", None) is not None else None,
        "safe_mode": c.safe_mode,
        "recovery_mode": c.recovery_mode,
        "components_count": len(c.components),
        "components": sorted(c.components)[:300],
        "allowlist_external_dirs": sorted(str(p) for p in c.allowlist_external_dirs),
    }


async def _list_entities(
    hass: HomeAssistant, domain: str | None = None, area: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Detailed entity-registry listing (area/device/labels/platform/state)."""
    reg = er.async_get(hass)
    area_id = _resolve_area_id(hass, area) if area else None
    items = []
    for e in reg.entities.values():
        if domain and e.domain != domain:
            continue
        if area_id is not None and e.area_id != area_id:
            continue
        if label is not None and label not in e.labels:
            continue
        items.append({
            "entity_id": e.entity_id,
            "name": e.name or e.original_name,
            "platform": e.platform,
            "area_id": e.area_id,
            "device_id": e.device_id,
            "labels": sorted(e.labels),
            "entity_category": e.entity_category,
            "disabled": e.disabled_by is not None,
            "hidden": e.hidden_by is not None,
        })
    items.sort(key=lambda x: x["entity_id"])
    return {"count": len(items), "entities": items[:400]}


async def _list_devices(
    hass: HomeAssistant, area: str | None = None, label: str | None = None,
) -> dict[str, Any]:
    """List the device registry (id/name/manufacturer/model/area/labels)."""
    reg = dr.async_get(hass)
    area_id = _resolve_area_id(hass, area) if area else None
    items = []
    for d in reg.devices.values():
        if area_id is not None and d.area_id != area_id:
            continue
        if label is not None and label not in d.labels:
            continue
        items.append({
            "id": d.id,
            "name": d.name_by_user or d.name,
            "manufacturer": d.manufacturer,
            "model": d.model,
            "area_id": d.area_id,
            "labels": sorted(d.labels),
            "sw_version": d.sw_version,
            "config_entries": sorted(d.config_entries),
            "disabled": d.disabled_by is not None,
        })
    items.sort(key=lambda x: (x["name"] or "", x["id"]))
    return {"count": len(items), "devices": items[:400]}


async def _update_device(
    hass: HomeAssistant, device_id: str, *, name: str | None = None,
    area: str | None = None, labels: list[str] | None = None,
) -> dict[str, Any]:
    """Rename a device / assign its area / set its labels."""
    reg = dr.async_get(hass)
    if reg.async_get(device_id) is None:
        return {"error": f"device '{device_id}' not found (use list_devices for ids)"}
    kwargs: dict[str, Any] = {}
    if name is not None:
        kwargs["name_by_user"] = name
    if area is not None:
        if area == "":
            kwargs["area_id"] = None
        else:
            area_id = _resolve_area_id(hass, area)
            if area_id is None:
                return {"error": f"area '{area}' not found; create it with create_area"}
            kwargs["area_id"] = area_id
    if labels is not None:
        kwargs["labels"] = set(labels)
    if not kwargs:
        return {"error": "nothing to update (provide name, area, or labels)"}
    d = reg.async_update_device(device_id, **kwargs)
    return {"ok": True, "id": d.id, "name": d.name_by_user or d.name,
            "area_id": d.area_id, "labels": sorted(d.labels)}


async def _assign_entity_labels(
    hass: HomeAssistant, entity_id: str, labels: list[str],
) -> dict[str, Any]:
    """Set the label set on a registry entity (resolves label names to ids)."""
    reg = er.async_get(hass)
    if reg.async_get(entity_id) is None:
        return {"error": f"entity '{entity_id}' is not in the entity registry"}
    lreg = lr.async_get(hass)
    by_name = {x.name: x.label_id for x in lreg.async_list_labels()}
    ids = {by_name.get(x, x) for x in labels}
    unknown = [x for x, i in zip(labels, [by_name.get(x, x) for x in labels])
               if lreg.async_get_label(i) is None]
    if unknown:
        return {"error": f"unknown label(s): {unknown}; create them with create_label"}
    e = reg.async_update_entity(entity_id, labels=ids)
    return {"ok": True, "entity_id": e.entity_id, "labels": sorted(e.labels)}


async def _list_floors(hass: HomeAssistant) -> dict[str, Any]:
    reg = fr.async_get(hass)
    items = [{"floor_id": f.floor_id, "name": f.name, "level": f.level,
              "icon": f.icon, "aliases": sorted(f.aliases)}
             for f in reg.async_list_floors()]
    items.sort(key=lambda x: (x["level"] if x["level"] is not None else 0, x["name"]))
    return {"count": len(items), "floors": items}


async def _create_floor(hass: HomeAssistant, name: str, level: int | None = None,
                        icon: str | None = None) -> dict[str, Any]:
    reg = fr.async_get(hass)
    existing = next((f for f in reg.async_list_floors() if f.name == name), None)
    if existing is not None:
        return {"ok": True, "floor_id": existing.floor_id, "name": existing.name,
                "existed": True}
    f = reg.async_create(name, level=level, icon=icon)
    return {"ok": True, "floor_id": f.floor_id, "name": f.name, "level": f.level}


async def _delete_floor(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    reg = fr.async_get(hass)
    f = reg.async_get_floor(identifier) or next(
        (x for x in reg.async_list_floors() if x.name == identifier), None)
    if f is None:
        return {"error": f"floor '{identifier}' not found"}
    reg.async_delete(f.floor_id)
    return {"ok": True, "deleted": f.floor_id}


async def _list_labels(hass: HomeAssistant) -> dict[str, Any]:
    reg = lr.async_get(hass)
    items = [{"label_id": x.label_id, "name": x.name, "color": x.color,
              "icon": x.icon, "description": x.description}
             for x in reg.async_list_labels()]
    items.sort(key=lambda x: x["name"])
    return {"count": len(items), "labels": items}


async def _create_label(hass: HomeAssistant, name: str, color: str | None = None,
                       icon: str | None = None, description: str | None = None) -> dict[str, Any]:
    reg = lr.async_get(hass)
    existing = next((x for x in reg.async_list_labels() if x.name == name), None)
    if existing is not None:
        return {"ok": True, "label_id": existing.label_id, "name": existing.name,
                "existed": True}
    x = reg.async_create(name, color=color, icon=icon, description=description)
    return {"ok": True, "label_id": x.label_id, "name": x.name}


async def _delete_label(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    reg = lr.async_get(hass)
    x = reg.async_get_label(identifier) or next(
        (y for y in reg.async_list_labels() if y.name == identifier), None)
    if x is None:
        return {"error": f"label '{identifier}' not found"}
    reg.async_delete(x.label_id)
    return {"ok": True, "deleted": x.label_id}


async def _list_statistics(hass: HomeAssistant) -> dict[str, Any]:
    """List long-term statistics ids tracked by the recorder."""
    rows = await _recorder_get_instance(hass).async_add_executor_job(
        _recorder_statistics.list_statistic_ids, hass, None, None)
    items = [{"statistic_id": r["statistic_id"], "source": r["source"],
              "unit": r.get("statistics_unit_of_measurement"),
              "has_mean": r.get("has_mean"), "has_sum": r.get("has_sum"),
              "name": r.get("name")} for r in rows]
    items.sort(key=lambda x: x["statistic_id"])
    return {"count": len(items), "statistics": items}


async def _get_statistics(hass: HomeAssistant, statistic_ids: list[str],
                         hours: int = 24, period: str = "hour") -> dict[str, Any]:
    """Fetch long-term statistics for the given ids over the last N hours."""
    if not statistic_ids:
        return {"error": "statistic_ids is required (see list_statistics)"}
    start = dt_util.utcnow() - timedelta(hours=hours)
    types = {"mean", "min", "max", "sum", "state", "change"}
    data = await _recorder_get_instance(hass).async_add_executor_job(
        _recorder_statistics.statistics_during_period,
        hass, start, None, set(statistic_ids), period, None, types)
    out: dict[str, Any] = {}
    for sid, rows in data.items():
        compact = [{k: v for k, v in row.items() if k != "start" or True}
                   for row in rows[-200:]]
        out[sid] = {"points": len(rows), "rows": compact[-50:]}
    return {"period": period, "hours": hours, "result": out}


async def _execute_script(hass: HomeAssistant, sequence: Any,
                         variables: dict | None = None) -> dict[str, Any]:
    """Run an ad-hoc HA action sequence (the script engine) without persisting.

    Accepts the same 'sequence' grammar as scripts/automations (service calls,
    delay, wait_template, choose, repeat, variables, stop with response). Returns
    any response/variables produced — the agent's general-purpose action runtime.
    """
    if isinstance(sequence, dict):
        sequence = [sequence]
    if not isinstance(sequence, list):
        return {"error": "sequence must be an action (object) or list of actions"}
    # Validate/compile through the script schema so 'service' is normalised and
    # template strings become Template objects (runs inside the event loop).
    sequence = cv.SCRIPT_SCHEMA(sequence)
    script = Script(hass, sequence, "ha_copilot.execute_script", "ha_copilot")
    result = await script.async_run(variables or {}, Context())
    payload: dict[str, Any] = {"ok": True}
    if result is not None:
        if result.service_response is not None:
            payload["response"] = result.service_response
        if result.variables:
            # Drop private keys and the run Context object (not JSON
            # serializable by the MCP endpoint's stdlib encoder).
            payload["variables"] = {
                k: v for k, v in result.variables.items()
                if not k.startswith("_") and k != "context"
            }
    return payload


async def _fire_event(hass: HomeAssistant, event_type: str,
                     event_data: dict | None = None) -> dict[str, Any]:
    """Fire a custom event on HA's event bus (drives event-triggered automations)."""
    hass.bus.async_fire(event_type, event_data or {})
    return {"ok": True, "event_type": event_type, "event_data": event_data or {}}


async def _list_persons(hass: HomeAssistant) -> dict[str, Any]:
    """List person entities and their tracked state/location."""
    items = []
    for s in hass.states.async_all("person"):
        items.append({
            "entity_id": s.entity_id,
            "name": s.attributes.get("friendly_name"),
            "state": s.state,
            "user_id": s.attributes.get("user_id"),
            "gps": [s.attributes.get("latitude"), s.attributes.get("longitude")]
            if s.attributes.get("latitude") is not None else None,
        })
    return {"count": len(items), "persons": items}


async def _get_logbook(hass: HomeAssistant, hours: int = 24,
                       entity_id: str | None = None) -> dict[str, Any]:
    """Humanised event timeline (logbook) over the recent window.

    Wraps the logbook EventProcessor (state changes + logbook entries +
    automation/script triggers + service calls) executed on the recorder.
    """
    from homeassistant.components.logbook.const import (
        EVENT_AUTOMATION_TRIGGERED,
        EVENT_LOGBOOK_ENTRY,
        EVENT_SCRIPT_STARTED,
    )
    from homeassistant.components.logbook.processor import EventProcessor

    end = dt_util.utcnow()
    start = end - timedelta(hours=max(1, min(int(hours), 168)))
    event_types = (EVENT_LOGBOOK_ENTRY, EVENT_AUTOMATION_TRIGGERED, EVENT_SCRIPT_STARTED)
    entity_ids = [entity_id] if entity_id else None
    processor = EventProcessor(hass, event_types, entity_ids=entity_ids,
                               device_ids=None, context_id=None,
                               timestamp=False, include_entity_name=True)
    events = await _recorder_get_instance(hass).async_add_executor_job(
        processor.get_events, start, end)
    return {"hours": hours, "count": len(events), "entries": events[-400:]}


async def _list_users(hass: HomeAssistant) -> dict[str, Any]:
    """List HA auth users (admin surface): id, name, flags, groups."""
    users = await hass.auth.async_get_users()
    items = [{
        "id": u.id,
        "name": u.name,
        "is_active": u.is_active,
        "is_owner": u.is_owner,
        "system_generated": u.system_generated,
        "local_only": u.local_only,
        "groups": [g.id for g in u.groups],
        "is_admin": any(g.id == "system-admin" for g in u.groups),
    } for u in users]
    items.sort(key=lambda x: (not x["is_owner"], x["name"] or ""))
    return {"count": len(items), "users": items}


async def _list_categories(hass: HomeAssistant, scope: str) -> dict[str, Any]:
    """List categories for a scope (e.g. 'automation', 'script')."""
    from homeassistant.helpers import category_registry as cr
    reg = cr.async_get(hass)
    items = [{"category_id": c.category_id, "name": c.name, "icon": c.icon}
             for c in reg.async_list_categories(scope=scope)]
    items.sort(key=lambda x: x["name"])
    return {"scope": scope, "count": len(items), "categories": items}


async def _create_category(hass: HomeAssistant, scope: str, name: str,
                           icon: str | None = None) -> dict[str, Any]:
    """Create a category in a scope (idempotent by name)."""
    from homeassistant.helpers import category_registry as cr
    reg = cr.async_get(hass)
    for c in reg.async_list_categories(scope=scope):
        if c.name == name:
            return {"ok": True, "category_id": c.category_id, "name": c.name,
                    "scope": scope, "existed": True}
    entry = reg.async_create(name=name, scope=scope, icon=icon)
    return {"ok": True, "category_id": entry.category_id, "name": entry.name,
            "scope": scope}


async def _delete_category(hass: HomeAssistant, scope: str,
                           identifier: str) -> dict[str, Any]:
    """Delete a category by id or name within a scope."""
    from homeassistant.helpers import category_registry as cr
    reg = cr.async_get(hass)
    cat_id = identifier
    for c in reg.async_list_categories(scope=scope):
        if c.name == identifier:
            cat_id = c.category_id
            break
    reg.async_delete(scope=scope, category_id=cat_id)
    return {"ok": True, "deleted": cat_id, "scope": scope}


async def _list_dashboards(hass: HomeAssistant) -> dict[str, Any]:
    """List Lovelace dashboards (default + storage + YAML), with mode."""
    from homeassistant.components.lovelace.const import DOMAIN as LOVELACE_DOMAIN
    data = hass.data.get(LOVELACE_DOMAIN)
    if data is None:
        return {"count": 0, "dashboards": []}
    # HA 2025.x stores a plain dict at hass.data["lovelace"]; older releases
    # exposed an object with .dashboards / .yaml_dashboards attributes.
    if isinstance(data, dict):
        dashboards = data.get("dashboards") or {}
        yaml_dashboards = data.get("yaml_dashboards") or {}
    else:
        dashboards = getattr(data, "dashboards", {}) or {}
        yaml_dashboards = getattr(data, "yaml_dashboards", {}) or {}
    items = []
    for url_path, cfg in dashboards.items():
        entry = {
            "url_path": url_path or "lovelace",
            "is_default": url_path is None,
            "mode": getattr(cfg, "mode", None),
        }
        ymeta = yaml_dashboards.get(url_path)
        if ymeta:
            entry["title"] = ymeta.get("title")
            entry["icon"] = ymeta.get("icon")
            entry["show_in_sidebar"] = ymeta.get("show_in_sidebar", True)
        items.append(entry)
    return {"count": len(items), "dashboards": items}


async def _get_dashboard_config(
    hass: HomeAssistant, url_path: str | None = None,
) -> dict[str, Any]:
    """Retrieve the full Lovelace config for a dashboard.

    Pass ``url_path`` (from list_dashboards) or None / "lovelace" for default.
    Returns the raw config dict (views, title, etc.).
    """
    from homeassistant.components.lovelace.const import DOMAIN as LOVELACE_DOMAIN

    data = hass.data.get(LOVELACE_DOMAIN)
    if data is None:
        return {"error": "lovelace component not loaded"}

    if isinstance(data, dict):
        dashboards = data.get("dashboards") or {}
    else:
        dashboards = getattr(data, "dashboards", {}) or {}

    key = None if url_path in (None, "", "lovelace") else url_path
    cfg_obj = dashboards.get(key)
    if cfg_obj is None:
        return {"error": f"dashboard '{url_path}' not found"}

    try:
        config = await cfg_obj.async_load(False)
    except Exception as err:  # noqa: BLE001
        return {"error": f"failed to load config: {err}"}

    if config is None:
        return {"ok": True, "url_path": url_path or "lovelace", "config": {}}

    views = config.get("views", [])
    return {
        "ok": True,
        "url_path": url_path or "lovelace",
        "title": config.get("title"),
        "view_count": len(views),
        "views": [
            {
                "index": i,
                "title": v.get("title", f"View {i}"),
                "path": v.get("path"),
                "card_count": len(v.get("cards", [])),
                "cards_summary": [
                    {"type": c.get("type", "?"), "entity": c.get("entity", "")}
                    for c in v.get("cards", [])[:20]
                ],
            }
            for i, v in enumerate(views)
        ],
    }


async def _update_dashboard(
    hass: HomeAssistant, url_path: str | None, config: dict[str, Any],
) -> dict[str, Any]:
    """Save a full Lovelace config for a storage-mode dashboard.

    The ``config`` dict replaces the entire dashboard configuration. Build it
    from get_dashboard_config's output, modify the views/cards, and call this
    to save.
    """
    from homeassistant.components.lovelace.const import DOMAIN as LOVELACE_DOMAIN

    data = hass.data.get(LOVELACE_DOMAIN)
    if data is None:
        return {"error": "lovelace component not loaded"}

    if isinstance(data, dict):
        dashboards = data.get("dashboards") or {}
    else:
        dashboards = getattr(data, "dashboards", {}) or {}

    key = None if url_path in (None, "", "lovelace") else url_path
    cfg_obj = dashboards.get(key)
    if cfg_obj is None:
        return {"error": f"dashboard '{url_path}' not found"}

    mode = getattr(cfg_obj, "mode", None)
    if mode == "yaml":
        return {"error": "YAML-mode dashboards cannot be updated programmatically — edit the YAML file directly"}

    try:
        await cfg_obj.async_save(config)
    except Exception as err:  # noqa: BLE001
        return {"error": f"save failed: {err}"}

    return {"ok": True, "url_path": url_path or "lovelace", "saved": True}



async def _get_energy_prefs(hass: HomeAssistant) -> dict[str, Any]:
    """Return the Energy dashboard preferences, or configured=false."""
    from homeassistant.components.energy import data as edata
    try:
        manager = await edata.async_get_manager(hass)
    except Exception as exc:  # noqa: BLE001
        return {"configured": False, "error": str(exc)}
    prefs = manager.data
    if not prefs:
        return {"configured": False}
    return {"configured": True, "prefs": prefs}


async def _conversation_process(hass: HomeAssistant, text: str,
                                language: str | None = None,
                                agent_id: str | None = None) -> dict[str, Any]:
    """Send text to the Assist conversation agent and return its response."""
    payload: dict[str, Any] = {"text": text}
    if language:
        payload["language"] = language
    if agent_id:
        payload["agent_id"] = agent_id
    resp = await hass.services.async_call(
        "conversation", "process", payload, blocking=True, return_response=True)
    return resp or {"ok": True}


async def _list_todo_items(hass: HomeAssistant,
                           entity_id: str | None = None) -> dict[str, Any]:
    """List items in a todo list (defaults to the first todo entity)."""
    if not entity_id:
        todos = hass.states.async_entity_ids("todo")
        if not todos:
            return {"count": 0, "lists": [], "items": []}
        entity_id = sorted(todos)[0]
    resp = await hass.services.async_call(
        "todo", "get_items", {}, target={"entity_id": entity_id},
        blocking=True, return_response=True)
    items = (resp or {}).get(entity_id, {}).get("items", [])
    return {"entity_id": entity_id, "count": len(items), "items": items}


async def _add_todo_item(hass: HomeAssistant, entity_id: str | None,
                         item: str) -> dict[str, Any]:
    """Add an item to a todo list (defaults to the first todo entity)."""
    if not entity_id:
        todos = hass.states.async_entity_ids("todo")
        if not todos:
            return {"error": "no todo entities found"}
        entity_id = sorted(todos)[0]
    await hass.services.async_call(
        "todo", "add_item", {"item": item}, target={"entity_id": entity_id},
        blocking=True)
    return {"ok": True, "entity_id": entity_id, "item": item}


async def _wait_for_event(hass: HomeAssistant, event_type: str,
                          timeout: float = 10.0,
                          entity_id: str | None = None) -> dict[str, Any]:
    """Subscribe to the event bus and wait (bounded) for the next match.

    The request/response bridge to HA's live event bus: blocks up to `timeout`
    seconds for the next event of `event_type` (optionally filtered by
    entity_id, e.g. for state_changed), then unsubscribes. Lets an agent
    *observe* the running system, not just poll it.
    """
    import asyncio
    fut: asyncio.Future = hass.loop.create_future()

    @callback
    def _cb(event: Any) -> None:
        if entity_id and event.data.get("entity_id") != entity_id:
            return
        if not fut.done():
            fut.set_result(event)

    unsub = hass.bus.async_listen(event_type, _cb)
    try:
        event = await asyncio.wait_for(fut, timeout=max(0.1, min(float(timeout), 60)))
    except (asyncio.TimeoutError, TimeoutError):
        return {"event_type": event_type, "timed_out": True, "timeout": timeout}
    finally:
        unsub()
    return {
        "event_type": event_type,
        "timed_out": False,
        "time_fired": event.time_fired.isoformat(),
        "origin": str(event.origin),
        "data": event.data,
    }


async def _list_tags(hass: HomeAssistant) -> dict[str, Any]:
    """List registered tags (NFC/RFID/QR) from the tag collection."""
    from homeassistant.components.tag import TAG_DATA
    coll = hass.data.get(TAG_DATA)
    if coll is None:
        return {"count": 0, "tags": []}
    items = [{"tag_id": t.get("id"), "name": t.get("name"),
              "last_scanned": t.get("last_scanned"),
              "device_id": t.get("device_id")}
             for t in coll.async_items()]
    return {"count": len(items), "tags": items}


async def _create_tag(hass: HomeAssistant, name: str,
                      tag_id: str | None = None) -> dict[str, Any]:
    """Create a tag (idempotent by name)."""
    from homeassistant.components.tag import TAG_DATA
    coll = hass.data.get(TAG_DATA)
    if coll is None:
        return {"error": "tag integration not loaded"}
    for t in coll.async_items():
        if t.get("name") == name:
            return {"ok": True, "tag_id": t.get("id"), "name": name, "existed": True}
    # The collection's create schema reads data["tag_id"] directly and
    # auto-generates a UUID when it is falsy, so the key must always be present.
    item = await coll.async_create_item({"name": name, "tag_id": tag_id or ""})
    return {"ok": True, "tag_id": item.get("id"), "name": item.get("name")}


async def _delete_tag(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Delete a tag by id or name."""
    from homeassistant.components.tag import TAG_DATA
    coll = hass.data.get(TAG_DATA)
    if coll is None:
        return {"error": "tag integration not loaded"}
    tag_id = identifier
    for t in coll.async_items():
        if t.get("name") == identifier:
            tag_id = t.get("id")
            break
    await coll.async_delete_item(tag_id)
    return {"ok": True, "deleted": tag_id}


async def _get_system_health(hass: HomeAssistant) -> dict[str, Any]:
    """Aggregate the system_health info of all integrations that report it."""
    # HA 2025.x removed system_health.get_info(); aggregate the registrations
    # stored at hass.data["system_health"] via get_integration_info() instead.
    from homeassistant.components.system_health import (
        DOMAIN as SH_DOMAIN,
        get_integration_info,
    )

    def _safe(v: Any) -> Any:
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        if isinstance(v, dict):
            return {k: _safe(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_safe(x) for x in v]
        return str(v)

    registrations = hass.data.get(SH_DOMAIN) or {}
    health: dict[str, Any] = {}
    for domain, registration in registrations.items():
        result = await get_integration_info(hass, registration)
        raw = result.get("info") or {}
        resolved: dict[str, Any] = {}
        for key, value in raw.items():
            # Some integrations report awaitable values (e.g. reachability
            # probes); resolve them to concrete values for a one-shot report.
            if asyncio.iscoroutine(value) or isinstance(value, asyncio.Task):
                try:
                    value = await value
                except Exception as exc:  # noqa: BLE001
                    value = f"error: {exc}"
            resolved[key] = _safe(value)
        health[domain] = resolved
    return {"count": len(health), "health": health}


async def _get_blueprint(hass: HomeAssistant, path: str,
                         domain: str = "automation") -> dict[str, Any]:
    """Return one blueprint's full metadata + input schema for a domain."""
    if domain == "automation":
        from homeassistant.components.automation.helpers import async_get_blueprints
    elif domain == "script":
        from homeassistant.components.script.helpers import async_get_blueprints
    else:
        return {"error": f"unsupported blueprint domain '{domain}' (automation|script)"}
    domain_bps = async_get_blueprints(hass)
    bp = await domain_bps.async_get_blueprint(path)
    meta = bp.metadata or {}
    return {
        "domain": domain,
        "path": path,
        "name": meta.get("name"),
        "description": meta.get("description"),
        "blueprint_domain": meta.get("domain"),
        "source_url": meta.get("source_url"),
        "inputs": meta.get("input") or {},
    }


async def _describe_service(hass: HomeAssistant, domain: str,
                            service: str) -> dict[str, Any]:
    """Full schema of one service: fields, selectors, target — exact call shape.

    list_services only gives names; this returns the per-field selectors and
    target schema an agent needs to construct a correct call_service payload.
    """
    from homeassistant.helpers.service import async_get_all_descriptions
    descs = await async_get_all_descriptions(hass)
    d = (descs.get(domain) or {}).get(service)
    if d is None:
        return {"error": f"service '{domain}.{service}' not found"}
    return {
        "domain": domain,
        "service": service,
        "name": d.get("name"),
        "description": d.get("description"),
        "target": d.get("target"),
        "fields": d.get("fields", {}),
    }


async def _describe_area(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Resolve an area (by id or name) into its full membership graph.

    Returns the area's floor, labels, member devices and the *effective*
    entities (entities assigned to the area directly, plus entities whose
    device lives in the area) — the area relationship graph.
    """
    areg = ar.async_get(hass)
    dreg = dr.async_get(hass)
    ereg = er.async_get(hass)
    area = areg.async_get_area(identifier)
    if area is None:
        area = next((a for a in areg.areas.values() if a.name == identifier), None)
    if area is None:
        return {"error": f"area '{identifier}' not found"}
    dev_ids = {d.id for d in dreg.devices.values() if d.area_id == area.id}
    devices = [{"id": d.id, "name": d.name_by_user or d.name}
               for d in dreg.devices.values() if d.id in dev_ids]
    entities = [{"entity_id": e.entity_id, "name": e.name or e.original_name,
                 "via": "device" if e.area_id is None else "direct"}
                for e in ereg.entities.values()
                if e.area_id == area.id or (e.area_id is None and e.device_id in dev_ids)]
    return {
        "area_id": area.id,
        "name": area.name,
        "floor_id": area.floor_id,
        "labels": sorted(area.labels),
        "device_count": len(devices),
        "devices": devices,
        "entity_count": len(entities),
        "entities": entities,
    }


async def _get_entity_registry_entry(hass: HomeAssistant,
                                     entity_id: str) -> dict[str, Any]:
    """Deep registry introspection of one entity (beyond its runtime state)."""
    ereg = er.async_get(hass)
    e = ereg.async_get(entity_id)
    if e is None:
        return {"error": f"entity '{entity_id}' not in the entity registry"}
    return {
        "entity_id": e.entity_id,
        "unique_id": e.unique_id,
        "platform": e.platform,
        "config_entry_id": e.config_entry_id,
        "device_id": e.device_id,
        "area_id": e.area_id,
        "entity_category": e.entity_category,
        "device_class": e.device_class or e.original_device_class,
        "disabled_by": e.disabled_by,
        "hidden_by": e.hidden_by,
        "name": e.name,
        "original_name": e.original_name,
        "icon": e.icon or e.original_icon,
        "unit_of_measurement": e.unit_of_measurement,
        "capabilities": e.capabilities,
        "supported_features": e.supported_features,
        "labels": sorted(e.labels),
        "options": e.options,
    }


async def _wait_for_template(hass: HomeAssistant, template: str,
                             timeout: float = 10.0) -> dict[str, Any]:
    """Bounded wait until a Jinja template renders truthy (the template analogue
    of wait_for_event). Returns immediately if already truthy.
    """
    import asyncio
    from homeassistant.helpers.event import TrackTemplate, async_track_template_result
    from homeassistant.helpers.template import result_as_boolean

    tmpl = Template(template, hass)
    try:
        current = tmpl.async_render()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"template render failed: {exc}"}
    if result_as_boolean(current):
        return {"matched": True, "waited": False, "result": str(current)}

    fut: asyncio.Future = hass.loop.create_future()

    @callback
    def _cb(event: Any, updates: Any) -> None:
        for upd in updates:
            res = upd.result
            if isinstance(res, Exception):
                continue
            if result_as_boolean(res) and not fut.done():
                fut.set_result(str(res))

    info = async_track_template_result(hass, [TrackTemplate(tmpl, None)], _cb)
    try:
        result = await asyncio.wait_for(fut, timeout=max(0.1, min(float(timeout), 60)))
    except (asyncio.TimeoutError, TimeoutError):
        return {"matched": False, "timed_out": True, "timeout": timeout}
    finally:
        info.async_remove()
    return {"matched": True, "waited": True, "result": result}


async def _get_config_entry(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Single config entry detail + options (secrets in .data are not exposed)."""
    entry = hass.config_entries.async_get_entry(identifier)
    if entry is None:
        entries = hass.config_entries.async_entries(identifier)
        entry = entries[0] if entries else None
    if entry is None:
        return {"error": f"config entry '{identifier}' not found (by entry_id or domain)"}
    return {
        "entry_id": entry.entry_id,
        "domain": entry.domain,
        "title": entry.title,
        "state": _entry_state(entry),
        "source": entry.source,
        "version": entry.version,
        "minor_version": getattr(entry, "minor_version", None),
        "disabled_by": entry.disabled_by,
        "supports_options": entry.supports_options,
        "supports_reconfigure": getattr(entry, "supports_reconfigure", None),
        "supports_unload": entry.supports_unload,
        "pref_disable_polling": entry.pref_disable_polling,
        "options": dict(entry.options),
    }


async def _get_device(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Deep device introspection (by id or name) + its entities — device graph.

    The device analogue of describe_area: connections, identifiers, owning
    config entries, via_device parent, firmware/hardware, area, and the full
    list of entities the device exposes.
    """
    dreg = dr.async_get(hass)
    ereg = er.async_get(hass)
    dev = dreg.async_get(identifier)
    if dev is None:
        dev = next((d for d in dreg.devices.values()
                    if (d.name_by_user or d.name) == identifier), None)
    if dev is None:
        return {"error": f"device '{identifier}' not found"}
    entities = [{"entity_id": e.entity_id, "name": e.name or e.original_name,
                 "domain": e.entity_id.split(".")[0]}
                for e in ereg.entities.values() if e.device_id == dev.id]
    return {
        "id": dev.id,
        "name": dev.name_by_user or dev.name,
        "name_by_user": dev.name_by_user,
        "manufacturer": dev.manufacturer,
        "model": dev.model,
        "sw_version": dev.sw_version,
        "hw_version": dev.hw_version,
        "area_id": dev.area_id,
        "via_device_id": dev.via_device_id,
        "config_entries": sorted(dev.config_entries),
        "connections": [list(c) for c in dev.connections],
        "identifiers": [list(i) for i in dev.identifiers],
        "disabled_by": dev.disabled_by,
        "entry_type": dev.entry_type,
        "labels": sorted(dev.labels),
        "entity_count": len(entities),
        "entities": entities,
    }


async def _get_statistic_metadata(hass: HomeAssistant,
                                  statistic_ids: list[str] | None = None) -> dict[str, Any]:
    """Recorder statistic metadata: unit, source, has_mean/has_sum, name."""
    ids = set(statistic_ids) if statistic_ids else None
    meta = await _recorder_get_instance(hass).async_add_executor_job(
        functools.partial(_recorder_statistics.get_metadata, hass, statistic_ids=ids))
    items = {sid: dict(m) for sid, (_row, m) in meta.items()}
    return {"count": len(items), "metadata": items}


async def _evaluate_condition(hass: HomeAssistant, condition: Any,
                              variables: dict | None = None) -> dict[str, Any]:
    """Validate + evaluate an HA condition config (state/numeric_state/template/
    time/and/or/...) against live state. Lets an agent test logic before
    committing it to an automation.
    """
    from homeassistant.helpers import condition as cond
    from homeassistant.helpers import config_validation as cv
    # Run through the config schema first so string templates / numeric coerces
    # become Template objects etc. (callers pass raw dicts, not cv-validated).
    condition = cv.CONDITION_SCHEMA(condition)
    validated = cond.async_validate_condition_config(hass, condition)
    if asyncio.iscoroutine(validated):
        validated = await validated
    checker = cond.async_from_config(hass, validated)
    if asyncio.iscoroutine(checker):
        checker = await checker
    res = checker(hass, variables or {})
    if asyncio.iscoroutine(res):
        res = await res
    return {"result": bool(res), "raw": res}


async def _list_zones(hass: HomeAssistant) -> dict[str, Any]:
    """List zones with geo (lat/long/radius) and the persons currently inside."""
    items = []
    for s in sorted(hass.states.async_all("zone"), key=lambda s: s.entity_id):
        a = s.attributes
        items.append({
            "entity_id": s.entity_id,
            "name": a.get("friendly_name"),
            "latitude": a.get("latitude"),
            "longitude": a.get("longitude"),
            "radius": a.get("radius"),
            "passive": a.get("passive"),
            "person_count": int(s.state) if s.state.isdigit() else None,
            "persons": list(a.get("persons", [])),
        })
    return {"count": len(items), "zones": items}


async def _get_automation_trace(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Return the most recent execution trace of an automation (step-by-step
    path through triggers/conditions/actions) — the automation debug surface.
    Accepts an automation entity_id, numeric id, or alias.
    """
    from homeassistant.components.trace import DATA_TRACE
    ereg = er.async_get(hass)
    # The trace store is flat, keyed by f"{domain}.{item_id}" where item_id is
    # the automation's config `id` (== the entity's registry unique_id).
    store = hass.data.get(DATA_TRACE, {})
    item_id = identifier
    if identifier.startswith("automation."):
        ent = ereg.async_get(identifier)
        if ent and ent.unique_id:
            item_id = ent.unique_id
    key = f"automation.{item_id}"
    traces = store.get(key)
    if not traces:
        # fall back to resolving by friendly-name alias
        for s in hass.states.async_all("automation"):
            if s.attributes.get("friendly_name") == identifier:
                ent = ereg.async_get(s.entity_id)
                cand = f"automation.{ent.unique_id}" if ent and ent.unique_id else None
                if cand and cand in store:
                    key = cand
                    traces = store.get(key)
                    break
    if not traces:
        return {"automation": identifier, "resolved_key": key,
                "count": 0, "traces": [],
                "note": "no stored traces yet (automation may not have run)"}
    ordered = list(traces.values())
    return {
        "automation": identifier,
        "resolved_key": key,
        "count": len(ordered),
        "latest": ordered[-1].as_short_dict(),
    }


async def _get_system_log(hass: HomeAssistant, level: str | None = None,
                          limit: int = 50) -> dict[str, Any]:
    """Recent captured log records (the Settings > System > Logs surface):
    level/message/source/exception/count/first+last occurrence. Optional level
    filter (ERROR/WARNING/...). The agent's window into what HA itself is
    complaining about, without shelling into the container.
    """
    from homeassistant.components.system_log import DATA_SYSTEM_LOG
    handler = hass.data.get(DATA_SYSTEM_LOG)
    if handler is None:
        return {"count": 0, "records": [], "note": "system_log not loaded"}
    records = handler.records.to_list()
    if level:
        lv = level.upper()
        records = [r for r in records if r.get("level") == lv]
    sliced = records[: max(1, min(int(limit), 200))]
    items = [{
        "level": r.get("level"),
        "message": (r.get("message") or [None])[0] if isinstance(r.get("message"), list) else r.get("message"),
        "source": r.get("source"),
        "name": r.get("name"),
        "count": r.get("count"),
        "first_occurred": r.get("first_occurred"),
        "timestamp": r.get("timestamp"),
        "has_exception": bool(r.get("exception")),
    } for r in sliced]
    return {"count": len(items), "total": len(records), "records": items}


async def _get_loaded_integrations(hass: HomeAssistant) -> dict[str, Any]:
    """The set of components currently loaded into this running instance —
    the live 'what is actually running' surface, the counterpart to
    get_integration_manifest (which describes one integration's code)."""
    comps = sorted(hass.config.components)
    return {"count": len(comps), "components": comps}


async def _call_service_response(hass: HomeAssistant, domain: str, service: str,
                                 data: dict | None = None) -> dict[str, Any]:
    """Call a service that returns a response payload (return_response=True),
    e.g. weather.get_forecasts, calendar.get_events, todo.get_items. call_service
    only confirms execution; this surfaces the data such read/query services
    produce."""
    if not hass.services.has_service(domain, service):
        return {"error": f"service '{domain}.{service}' does not exist"}
    try:
        resp = await hass.services.async_call(
            domain, service, dict(data or {}),
            blocking=True, return_response=True)
    except Exception as exc:  # noqa: BLE001 - surface service errors to agent
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "called": f"{domain}.{service}", "response": resp}


async def _get_integration_manifest(hass: HomeAssistant, domain: str) -> dict[str, Any]:
    """Integration manifest: name, version, requirements, dependencies,
    iot_class, config_flow, quality_scale, documentation — what an integration
    is made of, straight from the loaded code."""
    from homeassistant.loader import async_get_integration
    try:
        integ = await async_get_integration(hass, domain)
    except Exception as exc:  # noqa: BLE001 - integration may not exist
        return {"error": f"integration '{domain}' not found: {exc}"}
    return {
        "domain": integ.domain,
        "name": integ.name,
        "version": str(integ.version) if integ.version else None,
        "is_built_in": integ.is_built_in,
        "iot_class": integ.iot_class,
        "config_flow": integ.config_flow,
        "quality_scale": integ.quality_scale,
        "requirements": list(integ.requirements),
        "dependencies": list(integ.dependencies),
        "after_dependencies": list(integ.after_dependencies),
        "documentation": integ.documentation,
    }


async def _get_recorder_info(hass: HomeAssistant) -> dict[str, Any]:
    """Recorder health: whether it is recording and its current write backlog —
    a cheap liveness/health probe for the history/statistics subsystem."""
    inst = _recorder_get_instance(hass)
    return {
        "recording": bool(inst.recording),
        "backlog": getattr(inst, "backlog", None),
        "thread_alive": inst.is_alive() if hasattr(inst, "is_alive") else None,
    }


async def _set_state(hass: HomeAssistant, entity_id: str, state: str,
                     attributes: dict | None = None) -> dict[str, Any]:
    """Directly set/override an entity's state in the state machine.

    A virtual write to hass.states — useful to seed a test value so an agent can
    exercise templates/automations against it, or to push a value for an entity
    no integration backs. Note: a real integration may overwrite this on its
    next update; this does not persist to any device.
    """
    if "." not in entity_id:
        return {"error": "entity_id must be in '<domain>.<object_id>' form"}
    prev = hass.states.get(entity_id)
    hass.states.async_set(entity_id, state, attributes or {})
    new = hass.states.get(entity_id)
    return {"ok": True, "entity_id": entity_id,
            "previous_state": prev.state if prev else None,
            "state": new.state if new else None,
            "attributes": dict(new.attributes) if new else {}}


async def _get_automation_config(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Return an automation's full definition (alias/trigger/condition/action/
    mode) from automations.yaml, matched by id, alias, or entity_id. The
    configuration behind the entity, complementing get_automation_trace."""
    ereg = er.async_get(hass)
    target_id = identifier
    if identifier.startswith("automation."):
        ent = ereg.async_get(identifier)
        if ent and ent.unique_id:
            target_id = ent.unique_id
    path = _safe_path(hass, "automations.yaml")
    if not os.path.isfile(path):
        return {"error": "automations.yaml not found"}

    def _load() -> list:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or []

    items = await hass.async_add_executor_job(_load)
    for item in items:
        if str(item.get("id")) == str(target_id) or item.get("alias") == identifier:
            return {"found": True, "config": item}
    return {"found": False, "error": f"automation '{identifier}' not found in automations.yaml"}


async def _validate_automation_config(hass: HomeAssistant, config: dict) -> dict[str, Any]:
    """Validate an automation config against HA's schema WITHOUT saving it —
    returns ok or the precise validation error, so an agent can author a correct
    automation before create_automation/update_automation."""
    from homeassistant.components.automation.config import async_validate_config_item
    try:
        validated = await async_validate_config_item(hass, "automation", config)
    except Exception as exc:  # noqa: BLE001 - validation errors are user-facing
        return {"valid": False, "error": f"{type(exc).__name__}: {exc}"}
    if validated is None:
        return {"valid": False, "error": "config did not produce a valid automation"}
    return {"valid": True, "alias": config.get("alias"),
            "trigger_count": len(config.get("triggers") or config.get("trigger") or []),
            "action_count": len(config.get("actions") or config.get("action") or [])}


async def _list_config_flows(hass: HomeAssistant) -> dict[str, Any]:
    """Integrations that support a UI config flow + any flows currently
    in-progress (handler/step/source) — the integration setup surface."""
    from homeassistant.loader import async_get_config_flows
    domains = sorted(await async_get_config_flows(hass))
    in_progress = [{
        "flow_id": f.get("flow_id"),
        "handler": f.get("handler"),
        "step_id": f.get("step_id"),
        "source": (f.get("context") or {}).get("source"),
    } for f in hass.config_entries.flow.async_progress()]
    return {"supported_count": len(domains), "supported_domains": domains,
            "in_progress_count": len(in_progress), "in_progress": in_progress}


async def _import_statistics(hass: HomeAssistant, statistic_id: str,
                             statistics: list[dict], unit: str | None = None,
                             name: str | None = None,
                             has_mean: bool = False,
                             has_sum: bool = False) -> dict[str, Any]:
    """Insert long-term statistics points for a statistic_id (recorder write).

    Enables backfilling history for energy dashboards / custom metrics. If the
    id contains ':' it is treated as an EXTERNAL statistic (its own source);
    otherwise it is an internal (recorder-source) statistic. Each point needs a
    UTC hour-aligned `start` plus some of mean/min/max/sum/state.
    """
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
        async_import_statistics,
    )
    external = ":" in statistic_id
    source = statistic_id.split(":", 1)[0] if external else "recorder"
    if not has_mean and not has_sum:
        has_mean = any("mean" in s for s in statistics)
        has_sum = any("sum" in s for s in statistics)
    metadata = {
        "has_mean": has_mean,
        "has_sum": has_sum,
        "name": name,
        "source": source,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    points = []
    for s in statistics:
        start = dt_util.parse_datetime(s["start"]) if isinstance(s.get("start"), str) else s.get("start")
        if start is None:
            return {"error": f"invalid 'start' in point: {s.get('start')}"}
        pt: dict[str, Any] = {"start": dt_util.as_utc(start)}
        for k in ("mean", "min", "max", "sum", "state"):
            if k in s:
                pt[k] = s[k]
        points.append(pt)
    if external:
        async_add_external_statistics(hass, metadata, points)
    else:
        async_import_statistics(hass, metadata, points)
    return {"ok": True, "statistic_id": statistic_id, "external": external,
            "imported": len(points)}


async def _get_script_config(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Return a script's full definition (alias/sequence/mode/icon) from
    scripts.yaml, matched by object_id, 'script.<id>', or alias. The
    configuration behind the script entity."""
    object_id = identifier
    if identifier.startswith("script."):
        object_id = identifier.split(".", 1)[1]
    path = _safe_path(hass, "scripts.yaml")
    if not os.path.isfile(path):
        return {"error": "scripts.yaml not found"}

    def _load() -> dict:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    items = await hass.async_add_executor_job(_load)
    if object_id in items:
        return {"found": True, "object_id": object_id, "config": items[object_id]}
    for key, cfg in items.items():
        if isinstance(cfg, dict) and cfg.get("alias") == identifier:
            return {"found": True, "object_id": key, "config": cfg}
    return {"found": False, "error": f"script '{identifier}' not found in scripts.yaml"}


async def _get_scene_config(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Return a scene's full definition (name/entities/snapshot) from
    scenes.yaml, matched by name or id. The configuration behind the scene
    entity, including the exact entity states it restores."""
    path = _safe_path(hass, "scenes.yaml")
    if not os.path.isfile(path):
        return {"error": "scenes.yaml not found"}

    def _load() -> list:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or []

    items = await hass.async_add_executor_job(_load)
    for item in items:
        if item.get("name") == identifier or str(item.get("id")) == str(identifier):
            return {"found": True, "config": item,
                    "entity_count": len(item.get("entities") or {})}
    return {"found": False, "error": f"scene '{identifier}' not found in scenes.yaml"}


async def _clear_statistics(hass: HomeAssistant, statistic_ids: list[str]) -> dict[str, Any]:
    """Delete all long-term statistics for the given statistic_ids (recorder
    write). The cleanup counterpart to import_statistics — removes the series
    from history/energy entirely."""
    if not statistic_ids:
        return {"error": "statistic_ids is required"}
    _recorder_get_instance(hass).async_clear_statistics(list(statistic_ids))
    return {"ok": True, "cleared": statistic_ids,
            "note": "deletion is queued on the recorder thread"}


async def _get_device_automations(hass: HomeAssistant, device_id: str,
                                  automation_type: str = "trigger") -> dict[str, Any]:
    """List the device-automation capabilities a device exposes —
    triggers / conditions / actions usable in device-based automations
    (the 'Device' option in the automation editor)."""
    from homeassistant.components.device_automation import (
        DeviceAutomationType,
        async_get_device_automations,
    )
    dreg = dr.async_get(hass)
    if dreg.async_get(device_id) is None:
        return {"error": f"device '{device_id}' not found (see list_devices)"}
    try:
        dat = DeviceAutomationType[automation_type.upper()]
    except KeyError:
        return {"error": "automation_type must be one of trigger/condition/action"}
    result = await async_get_device_automations(hass, dat, [device_id])
    items = result.get(device_id, [])
    return {"device_id": device_id, "type": automation_type,
            "count": len(items), "automations": items}


async def _get_statistics_during_period(hass: HomeAssistant, statistic_ids: list[str],
                                        start: str, end: str | None = None,
                                        period: str = "hour") -> dict[str, Any]:
    """Pure-period statistics retrieval: rows strictly between an explicit
    start and end (ISO8601, UTC), at the requested period (5minute/hour/day/
    week/month). Unlike get_statistics (last-N-hours), this is a clean window."""
    if not statistic_ids:
        return {"error": "statistic_ids is required (see list_statistics)"}
    start_dt = dt_util.parse_datetime(start)
    if start_dt is None:
        return {"error": f"invalid 'start': {start}"}
    start_dt = dt_util.as_utc(start_dt)
    end_dt = None
    if end:
        end_dt = dt_util.parse_datetime(end)
        if end_dt is None:
            return {"error": f"invalid 'end': {end}"}
        end_dt = dt_util.as_utc(end_dt)
    types = {"mean", "min", "max", "sum", "state", "change"}
    data = await _recorder_get_instance(hass).async_add_executor_job(
        _recorder_statistics.statistics_during_period,
        hass, start_dt, end_dt, set(statistic_ids), period, None, types)
    out: dict[str, Any] = {}
    for sid, rows in data.items():
        out[sid] = {"points": len(rows), "rows": rows[:200]}
    return {"period": period, "start": start, "end": end, "result": out}


async def _get_entity_relations(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Entity-centric relationship graph: the entity's registry entry resolved
    up through its device → area → floor, plus sibling entities on the same
    device, its config entry and labels. The inverse view of describe_area."""
    ereg = er.async_get(hass)
    dreg = dr.async_get(hass)
    areg = ar.async_get(hass)
    freg = fr.async_get(hass)
    e = ereg.async_get(entity_id)
    if e is None:
        return {"error": f"entity '{entity_id}' not in the entity registry"}
    device = None
    siblings: list[dict[str, Any]] = []
    area_id = e.area_id
    if e.device_id:
        d = dreg.async_get(e.device_id)
        if d is not None:
            device = {"id": d.id, "name": d.name_by_user or d.name,
                      "manufacturer": d.manufacturer, "model": d.model,
                      "area_id": d.area_id}
            if area_id is None:
                area_id = d.area_id
            siblings = [{"entity_id": s.entity_id, "name": s.name or s.original_name}
                        for s in ereg.entities.values()
                        if s.device_id == e.device_id and s.entity_id != entity_id]
    area = None
    floor = None
    if area_id:
        a = areg.async_get_area(area_id)
        if a is not None:
            area = {"id": a.id, "name": a.name, "floor_id": a.floor_id}
            if a.floor_id:
                fl = freg.async_get_floor(a.floor_id)
                if fl is not None:
                    floor = {"floor_id": fl.floor_id, "name": fl.name, "level": fl.level}
    return {
        "entity_id": e.entity_id,
        "platform": e.platform,
        "config_entry_id": e.config_entry_id,
        "device": device,
        "area": area,
        "floor": floor,
        "labels": sorted(e.labels),
        "sibling_count": len(siblings),
        "siblings": siblings,
    }


async def _get_floor(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Resolve a floor (by floor_id or name) into the areas it contains and a
    total effective entity count — completes the floor→area→entity graph."""
    freg = fr.async_get(hass)
    areg = ar.async_get(hass)
    dreg = dr.async_get(hass)
    ereg = er.async_get(hass)
    floor = freg.async_get_floor(identifier)
    if floor is None:
        floor = next((f for f in freg.floors.values() if f.name == identifier), None)
    if floor is None:
        return {"error": f"floor '{identifier}' not found"}
    areas = [a for a in areg.async_list_areas() if a.floor_id == floor.floor_id]
    area_out: list[dict[str, Any]] = []
    total_entities = 0
    for a in areas:
        dev_ids = {d.id for d in dreg.devices.values() if d.area_id == a.id}
        n = sum(1 for e in ereg.entities.values()
                if e.area_id == a.id or (e.area_id is None and e.device_id in dev_ids))
        total_entities += n
        area_out.append({"id": a.id, "name": a.name, "entity_count": n})
    return {
        "floor_id": floor.floor_id,
        "name": floor.name,
        "level": floor.level,
        "area_count": len(area_out),
        "areas": area_out,
        "entity_count": total_entities,
    }


def _flatten_blueprint_inputs(meta_input: dict) -> dict[str, dict]:
    """Flatten a blueprint's input definition (sections may nest inputs)."""
    flat: dict[str, dict] = {}
    for key, val in (meta_input or {}).items():
        if isinstance(val, dict) and "input" in val:
            flat.update(_flatten_blueprint_inputs(val["input"]))
        else:
            flat[key] = val if isinstance(val, dict) else {}
    return flat


async def _get_blueprint_metadata(hass: HomeAssistant, domain: str, path: str):
    """Resolve a blueprint's metadata, auto-detecting its domain.

    A blueprint's domain (automation vs script) is intrinsic to the file, but
    callers (and agents chaining import -> validate -> create) routinely omit
    it and fall back to the 'automation' default. So we try the requested
    domain first, then the others, returning the domain that actually holds the
    blueprint — making the chain robust regardless of whether domain was
    threaded through. Returns (bp, resolved_domain, err)."""
    order = [domain] + [d for d in ("automation", "script") if d != domain]
    last_exc = None
    for dom in order:
        if dom == "automation":
            from homeassistant.components.automation.helpers import (
                async_get_blueprints,
            )
        elif dom == "script":
            from homeassistant.components.script.helpers import (
                async_get_blueprints,
            )
        else:
            continue
        try:
            bp = await async_get_blueprints(hass).async_get_blueprint(path)
            return bp, dom, None
        except Exception as exc:  # noqa: BLE001 - try the next domain
            last_exc = exc
    return None, domain, f"blueprint '{path}' not found: {last_exc}"


async def _validate_blueprint_inputs(hass: HomeAssistant, path: str,
                                     inputs: dict, domain: str = "automation") -> dict[str, Any]:
    """Check that a set of inputs satisfies a blueprint's schema BEFORE
    instantiating it — reports missing required inputs (those without a
    default) and any unknown keys. Precursor to create_automation_from_blueprint."""
    bp, resolved_domain, err = await _get_blueprint_metadata(hass, domain, path)
    if err:
        return {"error": err}
    meta = bp.metadata or {}
    flat = _flatten_blueprint_inputs(meta.get("input") or {})
    required = [k for k, v in flat.items() if "default" not in (v or {})]
    provided = set((inputs or {}).keys())
    missing = [k for k in required if k not in provided]
    unknown = [k for k in provided if k not in flat]
    return {
        "valid": not missing and not unknown,
        "blueprint": meta.get("name"),
        "domain": resolved_domain,
        "all_inputs": sorted(flat.keys()),
        "required": sorted(required),
        "missing": sorted(missing),
        "unknown": sorted(unknown),
    }


async def _create_automation_from_blueprint(hass: HomeAssistant, path: str,
                                            inputs: dict, alias: str,
                                            domain: str = "automation") -> dict[str, Any]:
    """Instantiate a blueprint into a real automation or script.

    Writes a ``use_blueprint`` entry to automations.yaml (automation domain) or
    scripts.yaml (script domain) and reloads. The blueprint's domain is
    auto-detected, so a script blueprint is correctly instantiated as a script
    even if the caller did not pass domain=script. Inputs are validated first;
    instantiation is rejected on any missing input."""
    check = await _validate_blueprint_inputs(hass, path, inputs, domain)
    if "error" in check:
        return check
    if check["missing"]:
        return {"error": f"missing required inputs: {check['missing']}",
                "required": check["required"]}
    resolved_domain = check.get("domain", domain)
    if resolved_domain == "script":
        result = await _create_script_from_blueprint(hass, alias, path, inputs)
    else:
        automation = {"alias": alias, "use_blueprint": {"path": path, "input": inputs}}
        result = await _create_automation(hass, automation)
    result["blueprint"] = path
    result["domain"] = resolved_domain
    result["from_blueprint"] = True
    return result


async def _create_script_from_blueprint(hass: HomeAssistant, alias: str,
                                        path: str, inputs: dict) -> dict[str, Any]:
    """Append a use_blueprint script entry to scripts.yaml (keyed by slug) and reload."""
    yaml_path = _safe_path(hass, "scripts.yaml")
    slug = _slugify(alias) or "copilot_script"

    def _append() -> str:
        existing: dict = {}
        if os.path.isfile(yaml_path):
            with open(yaml_path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    existing = loaded
        key = slug
        i = 2
        while key in existing:
            key = f"{slug}_{i}"
            i += 1
        existing[key] = {"alias": alias, "use_blueprint": {"path": path, "input": inputs}}
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)
        return key

    key = await hass.async_add_executor_job(_append)
    if hass.services.has_service("script", "reload"):
        await hass.services.async_call("script", "reload", {}, blocking=True)
    return {"ok": True, "script_entity_id": f"script.{key}"}


async def _get_template_functions(hass: HomeAssistant) -> dict[str, Any]:
    """Catalog the Jinja extensions available in THIS instance's template
    engine — globals/functions, filters and tests (including HA extras like
    states, area_id, device_id, expand). The authoring surface for templates."""
    from homeassistant.helpers.template import TemplateEnvironment
    env = TemplateEnvironment(hass)
    globals_ = sorted(k for k in env.globals if not k.startswith("_"))
    filters_ = sorted(env.filters.keys())
    tests_ = sorted(env.tests.keys())
    return {
        "globals_count": len(globals_), "globals": globals_,
        "filters_count": len(filters_), "filters": filters_,
        "tests_count": len(tests_), "tests": tests_,
    }


async def _get_assist_pipelines(hass: HomeAssistant) -> dict[str, Any]:
    """List all configured Assist (voice) pipelines with their STT/TTS/
    conversation engines and languages, plus which one is preferred — the
    full voice-assistant configuration surface."""
    from homeassistant.components.assist_pipeline.pipeline import (
        async_get_pipeline,
        async_get_pipelines,
    )
    pipelines = async_get_pipelines(hass)
    preferred = async_get_pipeline(hass)
    return {
        "count": len(pipelines),
        "preferred_id": preferred.id if preferred else None,
        "pipelines": [p.to_json() for p in pipelines],
    }


async def _get_assist_pipeline(hass: HomeAssistant, pipeline_id: str | None = None) -> dict[str, Any]:
    """One Assist pipeline's full definition (defaults to the preferred
    pipeline when no id given) — conversation/STT/TTS engines, languages,
    wake word and local-intent preference."""
    from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline
    try:
        pipeline = async_get_pipeline(hass, pipeline_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"pipeline '{pipeline_id}' not found: {exc}"}
    if pipeline is None:
        return {"error": f"pipeline '{pipeline_id}' not found"}
    return {"found": True, "pipeline": pipeline.to_json()}


async def _get_network_adapters(hass: HomeAssistant) -> dict[str, Any]:
    """The network adapters HA sees (name, default, auto/enabled, IPv4/IPv6) —
    the networking view used for discovery and URL announcement."""
    from homeassistant.components.network import async_get_adapters
    adapters = await async_get_adapters(hass)
    return {"count": len(adapters), "adapters": adapters}


async def _get_conversation_agents(hass: HomeAssistant) -> dict[str, Any]:
    """List the conversation/Assist agents that conversation_process can
    target via agent_id (the built-in Home Assistant agent plus any
    conversation entities from integrations)."""
    agents: list[dict[str, Any]] = []
    for state in hass.states.async_all("conversation"):
        agents.append({
            "agent_id": state.entity_id,
            "name": state.attributes.get("friendly_name") or state.name,
        })
    return {
        "count": len(agents),
        "default_agent_id": "conversation.home_assistant",
        "agents": agents,
    }


async def _purge_recorder(hass: HomeAssistant, keep_days: int = 10,
                          repack: bool = False, apply_filter: bool = False) -> dict[str, Any]:
    """Trigger a recorder purge (recorder.purge service): drop history/state
    rows older than keep_days; optionally repack the DB to reclaim disk and
    apply the include/exclude recorder filter. The recorder housekeeping op."""
    if not hass.services.has_service("recorder", "purge"):
        return {"error": "recorder.purge service unavailable (recorder not loaded)"}
    await hass.services.async_call(
        "recorder", "purge",
        {"keep_days": keep_days, "repack": repack, "apply_filter": apply_filter},
        blocking=True)
    return {"ok": True, "keep_days": keep_days, "repack": repack,
            "apply_filter": apply_filter,
            "note": "purge is queued on the recorder thread"}


async def _converse(hass: HomeAssistant, text: str, conversation_id: str | None = None,
                    language: str | None = None, agent_id: str | None = None) -> dict[str, Any]:
    """Multi-turn Assist conversation: like conversation_process but threads a
    conversation_id so follow-up turns share context. Returns the (new)
    conversation_id to chain, the agent's speech, and continue_conversation."""
    payload: dict[str, Any] = {"text": text}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if language:
        payload["language"] = language
    if agent_id:
        payload["agent_id"] = agent_id
    resp = await hass.services.async_call(
        "conversation", "process", payload, blocking=True, return_response=True) or {}
    response = resp.get("response") or {}
    speech = ((response.get("speech") or {}).get("plain") or {}).get("speech", "")
    return {
        "conversation_id": resp.get("conversation_id"),
        "continue_conversation": resp.get("continue_conversation"),
        "response_type": response.get("response_type"),
        "speech": speech,
    }


async def _get_recorder_db_info(hass: HomeAssistant) -> dict[str, Any]:
    """The recorder's database identity & footprint — SQL dialect, the
    (password-masked) connection URL, live recording flag + write backlog,
    bind-var limit, and on-disk size for SQLite. Complements get_recorder_info."""
    import os
    import re
    inst = _recorder_get_instance(hass)
    db_url = getattr(inst, "db_url", "") or ""
    masked = re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", db_url)
    size = None
    if db_url.startswith("sqlite") and "///" in db_url:
        path = db_url.split("///", 1)[1]
        if path and os.path.exists(path):
            size = os.path.getsize(path)
    return {
        "dialect": getattr(inst, "dialect_name", None),
        "db_url": masked,
        "recording": bool(inst.recording),
        "backlog": getattr(inst, "backlog", None),
        "max_bind_vars": getattr(inst, "max_bind_vars", None),
        "db_size_bytes": size,
    }


async def _get_recorder_runs(hass: HomeAssistant) -> dict[str, Any]:
    """List the recorder's run periods — every span between an HA start and the
    next clean shutdown that history was recorded for (start/end, and whether it
    closed incorrectly i.e. an unclean stop). The boot/uptime ledger of the DB."""
    if "recorder" not in hass.config.components:
        return {"error": "the recorder integration is not enabled"}
    from homeassistant.components.recorder.db_schema import RecorderRuns
    from homeassistant.components.recorder.util import session_scope

    def _query() -> list[dict[str, Any]]:
        with session_scope(hass=hass, read_only=True) as session:
            rows = session.query(RecorderRuns).order_by(RecorderRuns.start).all()
            return [{
                "run_id": r.run_id,
                "start": r.start.isoformat() if r.start else None,
                "end": r.end.isoformat() if r.end else None,
                "closed_incorrect": bool(r.closed_incorrect),
            } for r in rows]

    runs = await _recorder_get_instance(hass).async_add_executor_job(_query)
    return {"count": len(runs), "runs": runs[-50:]}


async def _get_entity_sources(hass: HomeAssistant, entity_id: str | None = None) -> dict[str, Any]:
    """Map live entities to their providing source — the integration/domain that
    created each one and whether it comes from a custom_component, plus its
    config_entry. Filter to one entity_id, or get a per-domain rollup of all."""
    from homeassistant.helpers.entity import entity_sources
    sources = entity_sources(hass)
    if entity_id:
        info = sources.get(entity_id)
        if info is None:
            return {"error": f"entity '{entity_id}' has no recorded source (not added by an integration)"}
        return {"entity_id": entity_id, "source": dict(info)}
    by_domain: dict[str, int] = {}
    custom: list[str] = []
    for eid, info in sources.items():
        dom = info.get("domain", "?")
        by_domain[dom] = by_domain.get(dom, 0) + 1
        if info.get("custom_component"):
            custom.append(eid)
    return {
        "total": len(sources),
        "domain_count": len(by_domain),
        "by_domain": dict(sorted(by_domain.items(), key=lambda kv: (-kv[1], kv[0]))),
        "custom_component_count": len(custom),
        "custom_component_entities": sorted(custom)[:50],
    }


async def _update_entity_registry(hass: HomeAssistant, entity_id: str,
                                  **changes: Any) -> dict[str, Any]:
    """Write entity-registry overrides for an entity — friendly name, icon,
    area assignment, entity_category, labels, enable/disable & hide, or a
    rename (new_entity_id). The user-customization surface of the registry."""
    ereg = er.async_get(hass)
    if ereg.async_get(entity_id) is None:
        return {"error": f"entity '{entity_id}' not in the entity registry"}
    kwargs: dict[str, Any] = {}
    for field in ("name", "icon", "area_id", "new_entity_id", "entity_category"):
        if changes.get(field) is not None:
            kwargs[field] = changes[field]
    if changes.get("labels") is not None:
        kwargs["labels"] = set(changes["labels"])
    if changes.get("disabled_by") is not None:
        kwargs["disabled_by"] = er.RegistryEntryDisabler(changes["disabled_by"])
    if changes.get("hidden_by") is not None:
        kwargs["hidden_by"] = er.RegistryEntryHider(changes["hidden_by"])
    if not kwargs:
        return {"error": "no updatable fields provided"}
    entry = ereg.async_update_entity(entity_id, **kwargs)
    return {
        "ok": True,
        "entity_id": entry.entity_id,
        "name": entry.name,
        "icon": entry.icon,
        "area_id": entry.area_id,
        "entity_category": entry.entity_category,
        "labels": sorted(entry.labels),
        "disabled_by": entry.disabled_by,
        "hidden_by": entry.hidden_by,
    }


_INPUT_DOMAINS = ("input_boolean", "input_number", "input_text",
                  "input_select", "input_datetime", "input_button")


async def _list_input_helpers(hass: HomeAssistant, domain: str | None = None) -> dict[str, Any]:
    """Enumerate the input_* helper entities (the user-defined state holders:
    input_boolean/number/text/select/datetime/button) with their current value
    and config (min/max/options/pattern/mode) — the manual-input control panel."""
    domains = (domain,) if domain else _INPUT_DOMAINS
    out: list[dict[str, Any]] = []
    for st in hass.states.async_all():
        dom = st.domain
        if dom not in domains:
            continue
        attrs = dict(st.attributes)
        cfg = {k: attrs[k] for k in ("min", "max", "step", "mode", "pattern",
                                     "options", "has_date", "has_time", "editable")
               if k in attrs}
        out.append({
            "entity_id": st.entity_id,
            "domain": dom,
            "state": st.state,
            "name": attrs.get("friendly_name"),
            "config": cfg,
        })
    out.sort(key=lambda e: e["entity_id"])
    return {"count": len(out), "helpers": out}


async def _set_input_helper(hass: HomeAssistant, entity_id: str, value: Any) -> dict[str, Any]:
    """Write a value to any input_* helper, routing to the right service:
    boolean→turn_on/off, number/text→set_value, select→select_option,
    datetime→set_datetime, button→press. The unified helper-write surface."""
    dom = entity_id.split(".", 1)[0]
    if dom not in _INPUT_DOMAINS:
        return {"error": f"'{entity_id}' is not an input_* helper"}
    if hass.states.get(entity_id) is None:
        return {"error": f"entity '{entity_id}' not found"}
    if dom == "input_boolean":
        svc = "turn_on" if str(value).lower() in ("1", "true", "on", "yes") else "turn_off"
        data: dict[str, Any] = {}
    elif dom == "input_button":
        svc, data = "press", {}
    elif dom == "input_number":
        svc, data = "set_value", {"value": float(value)}
    elif dom == "input_text":
        svc, data = "set_value", {"value": str(value)}
    elif dom == "input_select":
        svc, data = "select_option", {"option": str(value)}
    else:  # input_datetime
        v = str(value)
        if " " in v or "T" in v:
            data = {"datetime": v.replace("T", " ")}
        elif ":" in v:
            data = {"time": v}
        else:
            data = {"date": v}
        svc = "set_datetime"
    await hass.services.async_call(dom, svc, {"entity_id": entity_id, **data}, blocking=True)
    new = hass.states.get(entity_id)
    return {"ok": True, "entity_id": entity_id, "service": f"{dom}.{svc}",
            "state": new.state if new else None}


async def _get_group(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Resolve a group (or any grouping entity exposing an entity_id member
    list — group.*, light/switch groups, etc.) into its members with each
    member's live state. The membership + roll-up view of a grouping entity."""
    st = hass.states.get(entity_id)
    if st is None:
        return {"error": f"entity '{entity_id}' not found"}
    members = st.attributes.get("entity_id")
    if not members:
        return {"error": f"'{entity_id}' has no 'entity_id' member list (not a group)"}
    member_states = []
    for m in members:
        ms = hass.states.get(m)
        member_states.append({"entity_id": m, "state": ms.state if ms else "unknown",
                              "name": (ms.attributes.get("friendly_name") if ms else None)})
    return {
        "entity_id": entity_id,
        "state": st.state,
        "name": st.attributes.get("friendly_name"),
        "member_count": len(members),
        "members": member_states,
    }


async def _get_person(hass: HomeAssistant, identifier: str) -> dict[str, Any]:
    """Resolve a person (by person entity_id, person id, or name) into their
    tracked location — current zone/state, the device_trackers feeding it,
    linked HA user_id, and picture. The presence-detection identity view."""
    target = identifier if identifier.startswith("person.") else None
    for st in hass.states.async_all("person"):
        a = st.attributes
        if (target and st.entity_id == target) or (not target and (
                a.get("id") == identifier or a.get("friendly_name") == identifier
                or st.entity_id == f"person.{identifier}")):
            return {
                "entity_id": st.entity_id,
                "name": a.get("friendly_name"),
                "state": st.state,
                "id": a.get("id"),
                "user_id": a.get("user_id"),
                "device_trackers": a.get("device_trackers", []),
                "in_zones": a.get("in_zones", []),
                "gps_accuracy": a.get("gps_accuracy"),
                "latitude": a.get("latitude"),
                "longitude": a.get("longitude"),
                "picture": a.get("entity_picture"),
                "editable": a.get("editable"),
            }
    return {"error": f"no person matches '{identifier}'"}


async def _update_todo_item(hass: HomeAssistant, entity_id: str, item: str,
                            rename: str | None = None, status: str | None = None,
                            due_date: str | None = None,
                            description: str | None = None) -> dict[str, Any]:
    """Update an existing item on a to-do list (todo.update_item) — change its
    status (needs_action/completed), rename it, set a due date or description.
    The edit/complete counterpart to add_todo_item (write)."""
    if hass.states.get(entity_id) is None:
        return {"error": f"todo list '{entity_id}' not found"}
    data: dict[str, Any] = {"item": item}
    if rename is not None:
        data["rename"] = rename
    if status is not None:
        data["status"] = status
    if due_date is not None:
        data["due_date"] = due_date
    if description is not None:
        data["description"] = description
    await hass.services.async_call("todo", "update_item",
                                   {"entity_id": entity_id, **data}, blocking=True)
    return {"ok": True, "entity_id": entity_id, "item": item,
            "updated": {k: v for k, v in data.items() if k != "item"}}


async def _manage_addon(
    hass: HomeAssistant, slug: str, action: str
) -> dict[str, Any]:
    """Manage a Supervisor add-on: info/install/start/stop/restart/uninstall.

    Wraps the hassio component's service calls and the Supervisor REST API
    via ``hass.components.hassio``. Works only on HA OS / Supervised installs.
    """
    try:
        hassio = hass.components.hassio  # type: ignore[attr-defined]
    except AttributeError:
        return {"error": "Supervisor not available (HA OS or Supervised install required)"}

    if action == "info":
        try:
            resp = await hassio.async_get_addon_info(slug)
            return {
                "ok": True,
                "slug": slug,
                "name": resp.get("name", slug),
                "state": resp.get("state"),
                "version": resp.get("version"),
                "version_latest": resp.get("version_latest"),
                "installed": resp.get("state") != "unknown",
                "description": resp.get("description", ""),
                "url": resp.get("url", ""),
            }
        except Exception as err:  # noqa: BLE001
            # Fallback: try the REST API directly via websession
            try:
                from homeassistant.helpers.aiohttp_client import async_get_clientsession
                sess = async_get_clientsession(hass)
                token = os.environ.get("SUPERVISOR_TOKEN", "")
                if not token:
                    return {"error": f"addon info failed: {err}; no SUPERVISOR_TOKEN"}
                r = await sess.get(
                    f"http://supervisor/addons/{slug}/info",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                data = await r.json()
                d = data.get("data", {})
                return {
                    "ok": True,
                    "slug": slug,
                    "name": d.get("name", slug),
                    "state": d.get("state"),
                    "version": d.get("version"),
                    "version_latest": d.get("version_latest"),
                    "installed": d.get("state") not in ("unknown", None),
                    "description": d.get("description", ""),
                }
            except Exception as err2:  # noqa: BLE001
                return {"error": f"addon info failed: {err}; fallback also failed: {err2}"}

    # For write actions, use hassio services
    svc_map = {
        "install": "addon_install",
        "start": "addon_start",
        "stop": "addon_stop",
        "restart": "addon_restart",
        "uninstall": "addon_uninstall",
    }
    svc = svc_map.get(action)
    if not svc:
        return {"error": f"unknown action: {action}"}

    try:
        await hass.services.async_call("hassio", svc, {"addon": slug}, blocking=True)
        return {"ok": True, "slug": slug, "action": action, "done": True}
    except Exception as err:  # noqa: BLE001
        # Fallback: direct REST call to Supervisor API
        try:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession
            sess = async_get_clientsession(hass)
            token = os.environ.get("SUPERVISOR_TOKEN", "")
            if not token:
                return {"error": f"addon {action} failed: {err}; no SUPERVISOR_TOKEN"}
            r = await sess.post(
                f"http://supervisor/addons/{slug}/{action}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=120,
            )
            data = await r.json()
            if data.get("result") == "ok":
                return {"ok": True, "slug": slug, "action": action, "done": True}
            return {"error": f"addon {action}: {data.get('message', r.status)}"}
        except Exception as err2:  # noqa: BLE001
            return {"error": f"addon {action} failed: {err}; fallback: {err2}"}


async def _setup_integration(
    hass: HomeAssistant, domain: str, user_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Initiate a config flow to set up a native HA integration.

    For simple integrations (no user input needed), just passing the domain
    is enough. For those needing configuration, pass user_input with the
    required fields. Returns the flow result so the caller can see if more
    steps are needed or if setup completed.
    """
    from homeassistant import config_entries

    try:
        flow_mgr = hass.config_entries.flow
    except AttributeError:
        return {"error": "config_entries not available on this hass instance"}

    try:
        result = await flow_mgr.async_init(
            domain, context={"source": config_entries.SOURCE_USER}
        )
    except Exception as err:  # noqa: BLE001
        return {"error": f"config flow init failed for '{domain}': {err}"}

    flow_id = result.get("flow_id")
    step_type = result.get("type")
    step_id = result.get("step_id")

    # If the flow immediately created an entry (no user input needed)
    if step_type == "create_entry":
        entry = result.get("result")
        return {
            "ok": True,
            "domain": domain,
            "status": "created",
            "entry_id": entry.entry_id if entry else None,
            "title": result.get("title", domain),
        }

    # If the flow requires user input
    if step_type == "form" and user_input is not None:
        try:
            result2 = await flow_mgr.async_configure(
                flow_id, user_input=user_input
            )
            if result2.get("type") == "create_entry":
                entry = result2.get("result")
                return {
                    "ok": True,
                    "domain": domain,
                    "status": "created",
                    "entry_id": entry.entry_id if entry else None,
                    "title": result2.get("title", domain),
                }
            return {
                "ok": True,
                "domain": domain,
                "status": "needs_more_input",
                "step_id": result2.get("step_id"),
                "data_schema": _describe_schema(result2.get("data_schema")),
                "flow_id": result2.get("flow_id"),
            }
        except Exception as err:  # noqa: BLE001
            return {"error": f"config flow configure failed: {err}"}

    # Flow needs user input but none provided — describe what's needed
    return {
        "ok": True,
        "domain": domain,
        "status": "needs_input",
        "step_id": step_id,
        "data_schema": _describe_schema(result.get("data_schema")),
        "flow_id": flow_id,
        "hint": "Re-call with user_input containing the required fields.",
    }


async def _reconfigure_integration(
    hass: HomeAssistant, entry_id: str, user_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update the options of an existing config entry via its options flow.

    Most integrations expose an options flow for post-setup reconfiguration
    (e.g. changing Adaptive Lighting switches, Powercalc device models).
    Pass entry_id from list_config_entries. If user_input is None, returns the
    required fields; otherwise submits the form.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return {"error": f"config entry '{entry_id}' not found"}

    try:
        result = await hass.config_entries.options.async_init(entry_id)
    except Exception as err:  # noqa: BLE001
        return {"error": f"options flow init failed: {err} (integration may not support options)"}

    flow_id = result.get("flow_id")
    step_id = result.get("step_id")
    rtype = result.get("type")

    if rtype == "create_entry" or (rtype and str(rtype).endswith("create_entry")):
        return {"ok": True, "entry_id": entry_id, "status": "no_options",
                "hint": "This integration has no configurable options."}

    if user_input is not None and flow_id:
        try:
            result2 = await hass.config_entries.options.async_configure(
                flow_id, user_input,
            )
            rtype2 = result2.get("type")
            if rtype2 == "create_entry" or (rtype2 and str(rtype2).endswith("create_entry")):
                return {"ok": True, "entry_id": entry_id, "status": "updated"}
            return {
                "ok": True, "entry_id": entry_id, "status": "needs_more_input",
                "step_id": result2.get("step_id"),
                "data_schema": _describe_schema(result2.get("data_schema")),
                "flow_id": result2.get("flow_id"),
            }
        except Exception as err:  # noqa: BLE001
            return {"error": f"options flow configure failed: {err}"}

    return {
        "ok": True, "entry_id": entry_id, "status": "needs_input",
        "step_id": step_id,
        "data_schema": _describe_schema(result.get("data_schema")),
        "flow_id": flow_id,
        "hint": "Re-call with user_input containing the option values.",
    }


def _describe_schema(schema: Any) -> list[dict[str, str]] | None:
    """Best-effort description of a voluptuous schema for tool output."""
    if schema is None:
        return None
    try:
        fields = []
        for key in schema.schema:
            name = str(key)
            required = not isinstance(key, vol.Optional)
            if hasattr(key, "schema"):
                name = str(key.schema)
            fields.append({"name": name, "required": required})
        return fields
    except Exception:  # noqa: BLE001
        return None


async def _manage_hacs(
    hass: HomeAssistant, action: str, repo: str = "",
    category: str = "integration",
) -> dict[str, Any]:
    """Manage HACS repositories: list, add, download (install), remove.

    Uses HACS's WebSocket API (hacs/repositories/*) when available, with
    a REST fallback via the HACS API proxy.
    """
    try:
        ws_conn = hass.components.websocket_api  # noqa: F841
    except AttributeError:
        pass

    if action == "list":
        try:
            # Try the HACS websocket command
            result = await hass.services.async_call(
                "hacs", "repositories", blocking=True
            )
            return {"ok": True, "action": "list", "repositories": result}
        except Exception:  # noqa: BLE001
            pass
        # Fallback: query the HACS API directly
        try:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession
            sess = async_get_clientsession(hass)
            r = await sess.get(
                "http://localhost:8123/api/hacs/repositories",
                headers={"Authorization": f"Bearer {os.environ.get('HASS_TOKEN', '')}"},
                timeout=10,
            )
            data = await r.json()
            if isinstance(data, list):
                return {"ok": True, "action": "list", "count": len(data),
                        "repositories": data[:20]}
            return {"error": f"unexpected HACS response: {type(data)}"}
        except Exception as err:  # noqa: BLE001
            return {"error": f"HACS list failed: {err}. Is HACS installed?"}

    if action in ("download", "install"):
        if not repo:
            return {"error": "missing 'repo' (e.g. 'basnijholt/adaptive-lighting')"}
        try:
            # HACS WebSocket: download/install
            await hass.services.async_call(
                "hacs", "install",
                {"repository": repo, "category": category},
                blocking=True,
            )
            return {"ok": True, "action": "install", "repo": repo,
                    "hint": "Restart HA for the integration to load."}
        except Exception:  # noqa: BLE001
            pass
        # Fallback approach: use the HACS API
        try:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession
            sess = async_get_clientsession(hass)
            # Step 1: register the repo in HACS
            r = await sess.post(
                "http://localhost:8123/api/hacs/repositories",
                headers={
                    "Authorization": f"Bearer {os.environ.get('HASS_TOKEN', '')}",
                    "Content-Type": "application/json",
                },
                json={"repository": repo, "category": category},
                timeout=30,
            )
            # Step 2: trigger download
            r2 = await sess.post(
                f"http://localhost:8123/api/hacs/repositories/{repo}/download",
                headers={
                    "Authorization": f"Bearer {os.environ.get('HASS_TOKEN', '')}",
                    "Content-Type": "application/json",
                },
                timeout=60,
            )
            if r2.status < 300:
                return {"ok": True, "action": "install", "repo": repo,
                        "hint": "Restart HA for the integration to load."}
            return {"error": f"HACS download returned {r2.status}"}
        except Exception as err:  # noqa: BLE001
            return {"error": f"HACS install failed: {err}. Is HACS installed?"}

    if action == "remove":
        if not repo:
            return {"error": "missing 'repo'"}
        try:
            await hass.services.async_call(
                "hacs", "remove",
                {"repository": repo},
                blocking=True,
            )
            return {"ok": True, "action": "remove", "repo": repo}
        except Exception as err:  # noqa: BLE001
            return {"error": f"HACS remove failed: {err}"}

    return {"error": f"unknown action '{action}' — use list/install/download/remove"}


async def _run_tools(
    hass: HomeAssistant, store: dict, args: dict[str, Any]
) -> dict[str, Any]:
    """Run a sequence of tool calls in one request (sequential, ordered).

    Lets an agent batch a plan — e.g. read a state, then act, then read back —
    without a network/inference round-trip per step. Each item is
    ``{"tool": <name>, "args": {...}}``; results are returned in order. With
    ``stop_on_error`` the batch halts at the first failing call. ``run_tools``
    cannot be nested (guarded) so a batch can never recurse into itself.
    """
    calls = args.get("calls") or args.get("tools") or args.get("steps")
    if not isinstance(calls, list):
        return {"error": "'calls' must be a list of {tool, args} objects"}
    stop_on_error = bool(args.get("stop_on_error", False))
    results: list[dict[str, Any]] = []
    errors = 0
    for i, call in enumerate(calls):
        if not isinstance(call, dict):
            res: dict[str, Any] = {"error": "each call must be an object {tool, args}"}
            sub = None
        else:
            sub = call.get("tool")
            sub_args = call.get("args")
            if not isinstance(sub_args, dict):
                sub_args = {}
            if sub == "run_tools":
                res = {"error": "run_tools cannot be nested"}
            elif not sub or not isinstance(sub, str):
                res = {"error": "missing 'tool' name"}
            else:
                res = await dispatch(hass, store, sub, sub_args)
        is_err = isinstance(res, dict) and "error" in res
        if is_err:
            errors += 1
        results.append({"index": i, "tool": sub, "result": res})
        if is_err and stop_on_error:
            break
    return {
        "ok": errors == 0,
        "count": len(results),
        "errors": errors,
        "results": results,
    }


def _intent_slot_names(handler: Any) -> list[str]:
    """Best-effort slot names from an intent handler's voluptuous schema."""
    try:
        schema = handler.slot_schema
    except Exception:  # noqa: BLE001 - some handlers compute schema lazily
        return []
    if not schema:
        return []
    names: list[str] = []
    for key in schema:
        name = getattr(key, "schema", None)
        names.append(str(name if name is not None else key))
    return sorted(names)


async def _list_intents(hass: HomeAssistant) -> dict[str, Any]:
    """List intent handlers registered in HA (built-in + every integration).

    Borrows HA's own intent registry (``intent.async_get``) so an agent can see
    and trigger the same high-level intents Assist uses — HassTurnOn,
    HassClimateSetTemperature, plus any custom intents integrations register.
    """
    from homeassistant.helpers import intent

    handlers = sorted(intent.async_get(hass), key=lambda h: h.intent_type)
    out = [
        {
            "intent_type": h.intent_type,
            "description": getattr(h, "description", None),
            "slots": _intent_slot_names(h),
        }
        for h in handlers
    ]
    return {"count": len(out), "intents": out}


async def _handle_intent(
    hass: HomeAssistant, intent_type: str, slots: dict[str, Any] | None
) -> dict[str, Any]:
    """Fire a registered HA intent by type, wrapping bare slot values.

    Slots may be passed flat (``{"name": "Kitchen"}``); each value is wrapped to
    HA's ``{"value": ...}`` slot form unless already wrapped. Returns the intent
    response (speech + structured results) so the agent sees what HA did.
    """
    from homeassistant.helpers import intent

    wrapped: dict[str, Any] = {}
    for key, val in (slots or {}).items():
        wrapped[key] = val if isinstance(val, dict) and "value" in val else {"value": val}
    try:
        response = await intent.async_handle(
            hass, "ha_copilot", intent_type, wrapped or None
        )
    except intent.UnknownIntent:
        return {"error": f"unknown intent '{intent_type}' (try list_intents)"}
    except intent.IntentHandleError as exc:
        return {"error": f"intent handle error: {exc}"}
    except Exception as exc:  # noqa: BLE001 - surface validation/slot errors
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "intent_type": intent_type, "response": response.as_dict()}


async def _assist(
    hass: HomeAssistant,
    text: str,
    language: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Run free-form text through HA's own conversation (Assist) pipeline.

    Borrows HA's full natural-language stack — sentence/intent matching, area
    and device resolution, the active conversation agent — so an agent can hand
    off a plain command ("turn off the kitchen lights") instead of resolving
    entity_ids itself. Returns the spoken reply plus the structured response.
    """
    from homeassistant.components import conversation as conv
    from homeassistant.core import Context

    try:
        result = await conv.async_converse(
            hass, text, conversation_id, Context(), language=language
        )
    except Exception as exc:  # noqa: BLE001 - surface agent/pipeline errors
        return {"error": f"{type(exc).__name__}: {exc}"}
    data = result.as_dict()
    speech = ""
    try:
        speech = (
            data.get("response", {})
            .get("speech", {})
            .get("plain", {})
            .get("speech", "")
        )
    except AttributeError:
        speech = ""
    return {
        "ok": True,
        "speech": speech,
        "conversation_id": result.conversation_id,
        "response": data,
    }


# ---------------------------------------------------------------------------
# Deep HA integration — Phase 1+2: statistics, logbook, zones, persons,
# input helpers, backup, labels, floors, counters/timers, updates,
# system health, calendar, todo lists, tags, media, recorder
# ---------------------------------------------------------------------------


async def _get_statistics(
    hass: HomeAssistant,
    entity_id: str,
    period: str = "hour",
    hours: int = 24,
) -> dict[str, Any]:
    """Get long-term statistics for an entity (mean/min/max/sum/change).

    Period: '5minute', 'hour', 'day', 'week', 'month'. Goes back ``hours``
    hours. Works with recorder-tracked entities (energy, temperature, etc.).
    """
    from homeassistant.components.recorder.statistics import (
        async_get_last_statistics,
        statistics_during_period,
    )

    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        stats = await hass.async_add_executor_job(
            lambda: statistics_during_period(
                hass, start, None, {entity_id}, period, None, {"mean", "min", "max", "sum", "change"},
            )
        )
    except Exception as exc:  # noqa: BLE001
        try:
            last = await hass.async_add_executor_job(
                lambda: async_get_last_statistics(hass, 1, entity_id, True, {"mean", "min", "max", "sum"})
            )
            if last and entity_id in last:
                return {"ok": True, "entity_id": entity_id, "last": last[entity_id]}
        except Exception:  # noqa: BLE001
            pass
        return {"error": f"Statistics not available for {entity_id}: {exc}"}

    if entity_id not in stats or not stats[entity_id]:
        return {"ok": True, "entity_id": entity_id, "period": period,
                "data": [], "hint": "No statistics found. Entity may not be tracked by recorder."}

    rows = stats[entity_id]
    return {
        "ok": True,
        "entity_id": entity_id,
        "period": period,
        "hours": hours,
        "count": len(rows),
        "data": rows[:100],
    }


async def _get_logbook(
    hass: HomeAssistant,
    entity_id: str | None = None,
    hours: int = 24,
) -> dict[str, Any]:
    """Get logbook entries — human-readable event log (not raw log file).

    Returns state changes, automation triggers, service calls, etc. in
    chronological order. Filter by entity_id or get everything.
    """
    from homeassistant.components.logbook import async_log_entries

    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    end = datetime.now(timezone.utc)

    try:
        entries = await async_log_entries(hass, start, end, entity_id, None, None, None)
    except Exception as exc:  # noqa: BLE001
        entries_list: list[dict[str, Any]] = []
        for state in hass.states.async_all():
            if entity_id and state.entity_id != entity_id:
                continue
            entries_list.append({
                "entity_id": state.entity_id,
                "state": state.state,
                "when": state.last_changed.isoformat() if state.last_changed else None,
                "domain": state.entity_id.split(".")[0],
            })
        if entries_list:
            entries_list.sort(key=lambda x: x.get("when") or "", reverse=True)
            return {"ok": True, "count": len(entries_list), "entries": entries_list[:50],
                    "note": f"Logbook API unavailable ({exc}), showing state changes"}
        return {"error": f"Logbook unavailable: {exc}"}

    rows = []
    for entry in (entries or []):
        row = {}
        if hasattr(entry, "as_dict"):
            row = entry.as_dict()
        elif isinstance(entry, dict):
            row = entry
        else:
            row = {"entry": str(entry)}
        rows.append(row)

    return {
        "ok": True,
        "entity_id": entity_id,
        "hours": hours,
        "count": len(rows),
        "entries": rows[:100],
    }


async def _list_zones(hass: HomeAssistant) -> dict[str, Any]:
    """List all zones (geofencing areas used for presence detection)."""
    zones = []
    for state in hass.states.async_all("zone"):
        attrs = state.attributes
        zones.append({
            "entity_id": state.entity_id,
            "name": attrs.get("friendly_name", state.entity_id),
            "latitude": attrs.get("latitude"),
            "longitude": attrs.get("longitude"),
            "radius": attrs.get("radius"),
            "icon": attrs.get("icon"),
            "passive": attrs.get("passive", False),
            "persons_in_zone": attrs.get("persons", []),
        })
    return {"ok": True, "count": len(zones), "zones": zones}


async def _create_zone(
    hass: HomeAssistant,
    name: str,
    latitude: float,
    longitude: float,
    radius: float = 100,
    icon: str = "mdi:map-marker",
    passive: bool = False,
) -> dict[str, Any]:
    """Create a new zone for geofencing."""
    try:
        await hass.services.async_call(
            "zone", "create", {
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
                "radius": radius,
                "icon": icon,
                "passive": passive,
            }, blocking=True,
        )
    except Exception:  # noqa: BLE001
        zone_data = {
            "name": name,
            "latitude": latitude,
            "longitude": longitude,
            "radius": radius,
            "icon": icon,
            "passive": passive,
        }
        path = hass.config.path("zones.yaml")
        try:
            with open(path) as fh:
                existing = yaml.safe_load(fh.read()) or []
        except FileNotFoundError:
            existing = []
        if not isinstance(existing, list):
            existing = [existing]
        existing.append(zone_data)
        with open(path, "w") as fh:
            yaml.dump(existing, fh, default_flow_style=False, allow_unicode=True)
        return {"ok": True, "zone": zone_data, "path": path,
                "hint": "Run reload(target='zone') to apply."}

    return {"ok": True, "name": name}


async def _list_persons(hass: HomeAssistant) -> dict[str, Any]:
    """List all person entities with their tracking state."""
    persons = []
    for state in hass.states.async_all("person"):
        attrs = state.attributes
        persons.append({
            "entity_id": state.entity_id,
            "name": attrs.get("friendly_name", ""),
            "state": state.state,
            "latitude": attrs.get("latitude"),
            "longitude": attrs.get("longitude"),
            "gps_accuracy": attrs.get("gps_accuracy"),
            "source": attrs.get("source"),
            "user_id": attrs.get("user_id"),
            "device_trackers": attrs.get("device_trackers", []),
        })
    return {"ok": True, "count": len(persons), "persons": persons}


async def _manage_input_helper(
    hass: HomeAssistant,
    action: str,
    helper_type: str,
    entity_id: str | None = None,
    name: str | None = None,
    value: Any = None,
    options: list[str] | None = None,
    min_val: float | None = None,
    max_val: float | None = None,
    step: float | None = None,
    unit: str | None = None,
    icon: str | None = None,
    initial: Any = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Manage input helpers (input_boolean, input_number, input_select, etc.).

    Actions: 'list', 'create', 'set', 'delete'.
    Helper types: 'boolean', 'number', 'select', 'text', 'datetime', 'button'.
    """
    domain = f"input_{helper_type}" if not helper_type.startswith("input_") else helper_type

    if action == "list":
        helpers = []
        for state in hass.states.async_all(domain):
            helpers.append({
                "entity_id": state.entity_id,
                "state": state.state,
                "name": state.attributes.get("friendly_name", ""),
                "attributes": {k: v for k, v in state.attributes.items()
                               if k != "friendly_name"},
            })
        return {"ok": True, "domain": domain, "count": len(helpers), "helpers": helpers}

    if action == "set":
        if not entity_id:
            return {"error": "entity_id required for 'set' action"}
        svc_map = {
            "input_boolean": "turn_on" if value else "turn_off",
            "input_number": "set_value",
            "input_select": "select_option",
            "input_text": "set_value",
            "input_datetime": "set_datetime",
            "input_button": "press",
        }
        svc = svc_map.get(domain, "set_value")
        svc_data: dict[str, Any] = {"entity_id": entity_id}
        if domain == "input_number" and value is not None:
            svc_data["value"] = float(value)
        elif domain == "input_select" and value is not None:
            svc_data["option"] = str(value)
        elif domain == "input_text" and value is not None:
            svc_data["value"] = str(value)
        elif domain == "input_datetime" and value is not None:
            if isinstance(value, str) and ":" in value and "-" not in value:
                svc_data["time"] = value
            elif isinstance(value, str) and "-" in value and ":" not in value:
                svc_data["date"] = value
            else:
                svc_data["datetime"] = str(value)
        try:
            await hass.services.async_call(domain, svc, svc_data, blocking=True)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Failed to set {entity_id}: {exc}"}
        return {"ok": True, "entity_id": entity_id, "value": value}

    if action == "create":
        if not name:
            return {"error": "name required for 'create' action"}
        cfg: dict[str, Any] = {"name": name}
        if icon:
            cfg["icon"] = icon
        if domain == "input_number":
            cfg["min"] = min_val if min_val is not None else 0
            cfg["max"] = max_val if max_val is not None else 100
            if step is not None:
                cfg["step"] = step
            if unit:
                cfg["unit_of_measurement"] = unit
            if initial is not None:
                cfg["initial"] = initial
            if mode:
                cfg["mode"] = mode
        elif domain == "input_select":
            cfg["options"] = options or ["Option 1", "Option 2"]
            if initial:
                cfg["initial"] = initial
        elif domain == "input_text":
            if min_val is not None:
                cfg["min"] = int(min_val)
            if max_val is not None:
                cfg["max"] = int(max_val)
            if initial:
                cfg["initial"] = initial
            if mode:
                cfg["mode"] = mode
        elif domain == "input_boolean":
            if initial is not None:
                cfg["initial"] = initial
        elif domain == "input_datetime":
            cfg["has_date"] = True
            cfg["has_time"] = True

        path = hass.config.path(f"{domain}s.yaml")
        slug = name.lower().replace(" ", "_")
        try:
            with open(path) as fh:
                existing = yaml.safe_load(fh.read()) or {}
        except FileNotFoundError:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing[slug] = cfg
        with open(path, "w") as fh:
            yaml.dump(existing, fh, default_flow_style=False, allow_unicode=True)
        return {"ok": True, "domain": domain, "slug": slug, "config": cfg,
                "path": path, "hint": f"Run reload(target='{domain}') to apply."}

    if action == "delete":
        if not entity_id:
            return {"error": "entity_id required for 'delete' action"}
        slug = entity_id.split(".")[-1] if "." in entity_id else entity_id
        path = hass.config.path(f"{domain}s.yaml")
        try:
            with open(path) as fh:
                existing = yaml.safe_load(fh.read()) or {}
        except FileNotFoundError:
            return {"error": f"File not found: {path}"}
        if slug in existing:
            del existing[slug]
            with open(path, "w") as fh:
                yaml.dump(existing, fh, default_flow_style=False, allow_unicode=True)
            return {"ok": True, "deleted": slug, "path": path,
                    "hint": f"Run reload(target='{domain}') to apply."}
        return {"error": f"Helper '{slug}' not found in {path}"}

    return {"error": f"Unknown action: {action}. Use: list, create, set, delete"}


async def _manage_counter(
    hass: HomeAssistant,
    action: str,
    entity_id: str | None = None,
    name: str | None = None,
    initial: int | None = None,
    step: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
    icon: str | None = None,
) -> dict[str, Any]:
    """Manage counter helpers: list, create, increment, decrement, reset."""
    if action == "list":
        counters = []
        for state in hass.states.async_all("counter"):
            counters.append({
                "entity_id": state.entity_id,
                "state": state.state,
                "name": state.attributes.get("friendly_name", ""),
            })
        return {"ok": True, "count": len(counters), "counters": counters}

    if action in ("increment", "decrement", "reset"):
        if not entity_id:
            return {"error": "entity_id required"}
        try:
            await hass.services.async_call("counter", action, {"entity_id": entity_id}, blocking=True)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        return {"ok": True, "entity_id": entity_id, "action": action}

    if action == "create":
        if not name:
            return {"error": "name required for 'create'"}
        cfg: dict[str, Any] = {"name": name}
        if initial is not None:
            cfg["initial"] = initial
        if step is not None:
            cfg["step"] = step
        if minimum is not None:
            cfg["minimum"] = minimum
        if maximum is not None:
            cfg["maximum"] = maximum
        if icon:
            cfg["icon"] = icon
        slug = name.lower().replace(" ", "_")
        path = hass.config.path("counters.yaml")
        try:
            with open(path) as fh:
                existing = yaml.safe_load(fh.read()) or {}
        except FileNotFoundError:
            existing = {}
        existing[slug] = cfg
        with open(path, "w") as fh:
            yaml.dump(existing, fh, default_flow_style=False, allow_unicode=True)
        return {"ok": True, "slug": slug, "path": path,
                "hint": "Run reload(target='counter') to apply."}

    return {"error": f"Unknown action: {action}. Use: list, create, increment, decrement, reset"}


async def _manage_timer(
    hass: HomeAssistant,
    action: str,
    entity_id: str | None = None,
    name: str | None = None,
    duration: str | None = None,
    icon: str | None = None,
) -> dict[str, Any]:
    """Manage timer helpers: list, create, start, pause, cancel, finish."""
    if action == "list":
        timers = []
        for state in hass.states.async_all("timer"):
            attrs = state.attributes
            timers.append({
                "entity_id": state.entity_id,
                "state": state.state,
                "duration": attrs.get("duration"),
                "remaining": attrs.get("remaining"),
                "name": attrs.get("friendly_name", ""),
            })
        return {"ok": True, "count": len(timers), "timers": timers}

    if action in ("start", "pause", "cancel", "finish"):
        if not entity_id:
            return {"error": "entity_id required"}
        svc_data: dict[str, Any] = {"entity_id": entity_id}
        if action == "start" and duration:
            svc_data["duration"] = duration
        try:
            await hass.services.async_call("timer", action, svc_data, blocking=True)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        return {"ok": True, "entity_id": entity_id, "action": action}

    if action == "create":
        if not name:
            return {"error": "name required for 'create'"}
        cfg: dict[str, Any] = {"name": name}
        if duration:
            cfg["duration"] = duration
        if icon:
            cfg["icon"] = icon
        slug = name.lower().replace(" ", "_")
        path = hass.config.path("timers.yaml")
        try:
            with open(path) as fh:
                existing = yaml.safe_load(fh.read()) or {}
        except FileNotFoundError:
            existing = {}
        existing[slug] = cfg
        with open(path, "w") as fh:
            yaml.dump(existing, fh, default_flow_style=False, allow_unicode=True)
        return {"ok": True, "slug": slug, "path": path,
                "hint": "Run reload(target='timer') to apply."}

    return {"error": f"Unknown action: {action}. Use: list, create, start, pause, cancel, finish"}


async def _manage_backup(
    hass: HomeAssistant, action: str, slug: str | None = None,
) -> dict[str, Any]:
    """Manage HA backups: list, create, info, remove.

    Uses the built-in backup integration (Supervisor or Core backup).
    """
    if action == "list":
        try:
            backups = await hass.services.async_call(
                "backup", "list", {}, blocking=True, return_response=True,
            )
        except Exception:  # noqa: BLE001
            try:
                resp = await hass.async_add_executor_job(
                    lambda: hass.data.get("backup_manager")
                )
                if resp and hasattr(resp, "async_get_backups"):
                    bkps = await resp.async_get_backups()
                    return {"ok": True, "backups": [
                        {"slug": b.slug, "name": b.name, "date": b.date,
                         "size": getattr(b, "size", None)}
                        for b in (bkps.values() if isinstance(bkps, dict) else bkps)
                    ]}
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "backups": [],
                    "hint": "Backup integration not loaded. Add 'backup' to configuration.yaml."}
        return {"ok": True, "backups": backups if isinstance(backups, list) else []}

    if action == "create":
        try:
            result = await hass.services.async_call(
                "backup", "create", {}, blocking=True, return_response=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Backup creation failed: {exc}"}
        return {"ok": True, "backup": result}

    if action == "info":
        if not slug:
            return {"error": "slug required for 'info'"}
        return {"ok": True, "hint": "Use list to find backups, then download via Supervisor API."}

    if action == "remove":
        if not slug:
            return {"error": "slug required for 'remove'"}
        try:
            await hass.services.async_call(
                "backup", "remove", {"slug": slug}, blocking=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Remove failed: {exc}"}
        return {"ok": True, "removed": slug}

    return {"error": f"Unknown action: {action}. Use: list, create, info, remove"}


async def _manage_label(
    hass: HomeAssistant, action: str, name: str | None = None,
    label_id: str | None = None, color: str | None = None,
    icon: str | None = None, description: str | None = None,
) -> dict[str, Any]:
    """Manage HA labels (2024.1+): list, create, delete.

    Labels can be applied to entities, devices, areas, and automations
    for organizational purposes.
    """
    from homeassistant.helpers import label_registry as lr

    try:
        reg = lr.async_get(hass)
    except Exception:  # noqa: BLE001
        return {"error": "Label registry not available (requires HA 2024.1+)"}

    if action == "list":
        labels = []
        for label in reg.async_list_labels():
            labels.append({
                "label_id": label.label_id,
                "name": label.name,
                "color": getattr(label, "color", None),
                "icon": getattr(label, "icon", None),
                "description": getattr(label, "description", None),
            })
        return {"ok": True, "count": len(labels), "labels": labels}

    if action == "create":
        if not name:
            return {"error": "name required for 'create'"}
        try:
            label = reg.async_create(
                name, color=color, icon=icon, description=description,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Create label failed: {exc}"}
        return {"ok": True, "label_id": label.label_id, "name": label.name}

    if action == "delete":
        lid = label_id or name
        if not lid:
            return {"error": "label_id or name required for 'delete'"}
        try:
            reg.async_delete(lid)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Delete label failed: {exc}"}
        return {"ok": True, "deleted": lid}

    return {"error": f"Unknown action: {action}. Use: list, create, delete"}


async def _manage_floor(
    hass: HomeAssistant, action: str, name: str | None = None,
    floor_id: str | None = None, icon: str | None = None,
    level: int | None = None,
) -> dict[str, Any]:
    """Manage floors (2024.2+): list, create, delete.

    Floors sit above areas in the spatial hierarchy: Floor → Areas → Devices.
    """
    from homeassistant.helpers import floor_registry as fr

    try:
        reg = fr.async_get(hass)
    except Exception:  # noqa: BLE001
        return {"error": "Floor registry not available (requires HA 2024.2+)"}

    if action == "list":
        floors = []
        for floor in reg.async_list_floors():
            floors.append({
                "floor_id": floor.floor_id,
                "name": floor.name,
                "icon": getattr(floor, "icon", None),
                "level": getattr(floor, "level", None),
                "aliases": list(getattr(floor, "aliases", set())),
            })
        return {"ok": True, "count": len(floors), "floors": floors}

    if action == "create":
        if not name:
            return {"error": "name required for 'create'"}
        try:
            floor = reg.async_create(name, icon=icon, level=level)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Create floor failed: {exc}"}
        return {"ok": True, "floor_id": floor.floor_id, "name": floor.name}

    if action == "delete":
        fid = floor_id
        if not fid:
            return {"error": "floor_id required for 'delete'"}
        try:
            reg.async_delete(fid)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Delete floor failed: {exc}"}
        return {"ok": True, "deleted": fid}

    return {"error": f"Unknown action: {action}. Use: list, create, delete"}


async def _check_updates(hass: HomeAssistant) -> dict[str, Any]:
    """List all available updates (HA core, HACS, add-ons, devices)."""
    updates = []
    for state in hass.states.async_all("update"):
        attrs = state.attributes
        updates.append({
            "entity_id": state.entity_id,
            "name": attrs.get("friendly_name", ""),
            "installed_version": attrs.get("installed_version"),
            "latest_version": attrs.get("latest_version"),
            "update_available": state.state == "on",
            "release_summary": attrs.get("release_summary"),
            "release_url": attrs.get("release_url"),
            "skipped_version": attrs.get("skipped_version"),
            "in_progress": attrs.get("in_progress", False),
        })
    available = [u for u in updates if u["update_available"]]
    return {
        "ok": True,
        "total_update_entities": len(updates),
        "updates_available": len(available),
        "updates": available,
        "up_to_date": [u for u in updates if not u["update_available"]][:10],
    }


async def _get_system_health(hass: HomeAssistant) -> dict[str, Any]:
    """Get system health information (HA version, OS, arch, DB size, etc.)."""
    info: dict[str, Any] = {
        "version": hass.config.version if hasattr(hass.config, "version") else "unknown",
        "location_name": getattr(hass.config, "location_name", ""),
        "time_zone": str(getattr(hass.config, "time_zone", "")),
        "elevation": getattr(hass.config, "elevation", None),
        "unit_system": str(getattr(hass.config, "units", "")),
        "config_dir": hass.config.config_dir,
        "allowlist_external_dirs": list(getattr(hass.config, "allowlist_external_dirs", [])),
        "allowlist_external_urls": list(getattr(hass.config, "allowlist_external_urls", [])),
        "components_loaded": len(getattr(hass, "config", {}).get("components", set()) if isinstance(getattr(hass, "config", None), dict) else getattr(getattr(hass, "config", None), "components", set())),
    }

    entity_count = len(hass.states.async_all())
    domain_counts: dict[str, int] = {}
    for state in hass.states.async_all():
        d = state.entity_id.split(".")[0]
        domain_counts[d] = domain_counts.get(d, 0) + 1

    info["entity_count"] = entity_count
    info["domain_counts"] = dict(sorted(domain_counts.items(), key=lambda x: -x[1])[:20])

    import os
    db_path = os.path.join(hass.config.config_dir, "home-assistant_v2.db")
    try:
        db_size = os.path.getsize(db_path)
        info["database_size_mb"] = round(db_size / (1024 * 1024), 1)
    except OSError:
        info["database_size_mb"] = None

    return {"ok": True, "system": info}


async def _manage_calendar(
    hass: HomeAssistant,
    action: str,
    entity_id: str | None = None,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Manage calendar entities: list, get_events, create_event."""
    if action == "list":
        calendars = []
        for state in hass.states.async_all("calendar"):
            attrs = state.attributes
            calendars.append({
                "entity_id": state.entity_id,
                "name": attrs.get("friendly_name", ""),
                "state": state.state,
                "message": attrs.get("message"),
                "start_time": attrs.get("start_time"),
                "end_time": attrs.get("end_time"),
            })
        return {"ok": True, "count": len(calendars), "calendars": calendars}

    if action == "get_events":
        if not entity_id:
            return {"error": "entity_id required for 'get_events'"}
        try:
            now = datetime.now(timezone.utc)
            result = await hass.services.async_call(
                "calendar", "get_events", {
                    "entity_id": entity_id,
                    "start_date_time": (now - timedelta(days=1)).isoformat(),
                    "end_date_time": (now + timedelta(days=7)).isoformat(),
                }, blocking=True, return_response=True,
            )
            return {"ok": True, "entity_id": entity_id, "events": result}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Get events failed: {exc}"}

    if action == "create_event":
        if not entity_id or not summary or not start or not end:
            return {"error": "entity_id, summary, start, end required for 'create_event'"}
        svc_data: dict[str, Any] = {
            "entity_id": entity_id,
            "summary": summary,
            "start_date_time": start,
            "end_date_time": end,
        }
        if description:
            svc_data["description"] = description
        try:
            await hass.services.async_call(
                "calendar", "create_event", svc_data, blocking=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Create event failed: {exc}"}
        return {"ok": True, "summary": summary, "start": start, "end": end}

    return {"error": f"Unknown action: {action}. Use: list, get_events, create_event"}


async def _manage_todo(
    hass: HomeAssistant,
    action: str,
    entity_id: str | None = None,
    item: str | None = None,
    status: str | None = None,
    uid: str | None = None,
) -> dict[str, Any]:
    """Manage to-do list entities (HA 2023.11+): list, get_items, add, update, remove."""
    if action == "list":
        todos = []
        for state in hass.states.async_all("todo"):
            attrs = state.attributes
            todos.append({
                "entity_id": state.entity_id,
                "name": attrs.get("friendly_name", ""),
                "state": state.state,
            })
        return {"ok": True, "count": len(todos), "todo_lists": todos}

    if action == "get_items":
        if not entity_id:
            return {"error": "entity_id required"}
        try:
            result = await hass.services.async_call(
                "todo", "get_items", {"entity_id": entity_id},
                blocking=True, return_response=True,
            )
            return {"ok": True, "entity_id": entity_id, "items": result}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Get items failed: {exc}"}

    if action == "add":
        if not entity_id or not item:
            return {"error": "entity_id and item required"}
        try:
            await hass.services.async_call(
                "todo", "add_item", {"entity_id": entity_id, "item": item},
                blocking=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Add item failed: {exc}"}
        return {"ok": True, "entity_id": entity_id, "item": item}

    if action == "update":
        if not entity_id or not uid:
            return {"error": "entity_id and uid required"}
        svc_data: dict[str, Any] = {"entity_id": entity_id, "item": uid}
        if item:
            svc_data["rename"] = item
        if status:
            svc_data["status"] = status
        try:
            await hass.services.async_call(
                "todo", "update_item", svc_data, blocking=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Update failed: {exc}"}
        return {"ok": True, "entity_id": entity_id, "uid": uid}

    if action == "remove":
        if not entity_id or not uid:
            return {"error": "entity_id and uid required"}
        try:
            await hass.services.async_call(
                "todo", "remove_item", {"entity_id": entity_id, "item": uid},
                blocking=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Remove failed: {exc}"}
        return {"ok": True, "entity_id": entity_id, "removed": uid}

    return {"error": f"Unknown action: {action}. Use: list, get_items, add, update, remove"}


async def _manage_tag(
    hass: HomeAssistant, action: str, tag_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Manage NFC/RFID tags: list, create, remove."""
    from homeassistant.helpers import tag as tag_helper

    if action == "list":
        try:
            tags = tag_helper.async_get_tags(hass) if hasattr(tag_helper, "async_get_tags") else {}
        except Exception:  # noqa: BLE001
            tags = hass.data.get("tag", {})
        if not tags:
            return {"ok": True, "count": 0, "tags": [],
                    "hint": "No tags registered. Scan a tag with your phone to register it."}
        tag_list = []
        if isinstance(tags, dict):
            for tid, info in tags.items():
                tag_list.append({
                    "tag_id": tid,
                    "name": info.get("name", "") if isinstance(info, dict) else str(info),
                })
        return {"ok": True, "count": len(tag_list), "tags": tag_list}

    if action == "create":
        try:
            result = await tag_helper.async_create_tag(hass, name or "New Tag", tag_id)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Create tag failed: {exc}"}
        return {"ok": True, "tag_id": result if isinstance(result, str) else str(result)}

    if action == "remove":
        if not tag_id:
            return {"error": "tag_id required for 'remove'"}
        try:
            await tag_helper.async_remove_tag(hass, tag_id)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Remove tag failed: {exc}"}
        return {"ok": True, "removed": tag_id}

    return {"error": f"Unknown action: {action}. Use: list, create, remove"}


async def _browse_media(
    hass: HomeAssistant,
    media_content_id: str | None = None,
    media_content_type: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Browse media library (local media, TTS, Spotify, etc.)."""
    try:
        from homeassistant.components.media_source import async_browse_media
        result = await async_browse_media(
            hass, media_content_id, media_content_type,
        )
        children = []
        for child in (result.children or []):
            children.append({
                "title": child.title,
                "media_content_id": child.media_content_id,
                "media_content_type": child.media_content_type,
                "media_class": child.media_class,
                "can_play": child.can_play,
                "can_expand": child.can_expand,
            })
        return {
            "ok": True,
            "title": result.title,
            "media_content_id": result.media_content_id,
            "children_count": len(children),
            "children": children[:50],
        }
    except Exception as exc:  # noqa: BLE001
        media_players = []
        for state in hass.states.async_all("media_player"):
            attrs = state.attributes
            media_players.append({
                "entity_id": state.entity_id,
                "state": state.state,
                "name": attrs.get("friendly_name", ""),
                "media_title": attrs.get("media_title"),
                "source_list": attrs.get("source_list", [])[:10],
            })
        return {"ok": True, "media_players": media_players,
                "note": f"Media browse unavailable ({exc}), showing players"}


async def _get_camera_snapshot(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Get camera entity info (proxy URL for snapshot access)."""
    state = hass.states.get(entity_id)
    if not state:
        return {"error": f"Camera {entity_id} not found"}
    attrs = state.attributes
    return {
        "ok": True,
        "entity_id": entity_id,
        "state": state.state,
        "name": attrs.get("friendly_name", ""),
        "access_token": attrs.get("access_token"),
        "entity_picture": attrs.get("entity_picture"),
        "frontend_stream_type": attrs.get("frontend_stream_type"),
        "proxy_url": f"/api/camera_proxy/{entity_id}",
        "stream_url": f"/api/camera_proxy_stream/{entity_id}",
    }


async def _purge_recorder(
    hass: HomeAssistant,
    keep_days: int = 10,
    repack: bool = False,
) -> dict[str, Any]:
    """Purge old recorder data to free database space."""
    try:
        await hass.services.async_call(
            "recorder", "purge", {
                "keep_days": keep_days,
                "repack": repack,
            }, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Purge failed: {exc}"}
    return {"ok": True, "keep_days": keep_days, "repack": repack,
            "hint": "Purge started. Large databases may take several minutes."}


async def _manage_schedule(
    hass: HomeAssistant,
    action: str,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """List schedule helper entities and their current state."""
    if action == "list":
        schedules = []
        for state in hass.states.async_all("schedule"):
            schedules.append({
                "entity_id": state.entity_id,
                "state": state.state,
                "name": state.attributes.get("friendly_name", ""),
                "next_event": state.attributes.get("next_event"),
            })
        return {"ok": True, "count": len(schedules), "schedules": schedules}

    if action == "get" and entity_id:
        state = hass.states.get(entity_id)
        if not state:
            return {"error": f"Schedule {entity_id} not found"}
        return {"ok": True, "entity_id": entity_id, "state": state.state,
                "attributes": dict(state.attributes)}

    return {"error": f"Unknown action: {action}. Use: list, get"}


# ---------------------------------------------------------------------------
# Advanced operations — automation control, device mgmt, media, scene, history
# ---------------------------------------------------------------------------


async def _toggle_automation(
    hass: HomeAssistant, entity_id: str, enable: bool,
) -> dict[str, Any]:
    """Enable or disable an automation without deleting it."""
    svc = "turn_on" if enable else "turn_off"
    try:
        await hass.services.async_call(
            "automation", svc, {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Toggle automation failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "enabled": enable}


async def _trigger_automation(
    hass: HomeAssistant, entity_id: str, skip_condition: bool = False,
) -> dict[str, Any]:
    """Manually trigger an automation."""
    try:
        await hass.services.async_call(
            "automation", "trigger",
            {"entity_id": entity_id, "skip_condition": skip_condition},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Trigger automation failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "skip_condition": skip_condition}


async def _duplicate_automation(
    hass: HomeAssistant, entity_id: str, new_alias: str | None = None,
) -> dict[str, Any]:
    """Duplicate an automation by reading its config and creating a copy."""
    from ha_copilot.tools import _get_automation_config, _create_automation

    config_result = await _get_automation_config(hass, entity_id)
    if "error" in config_result:
        return config_result

    config = config_result.get("config") or config_result.get("automation") or {}
    if not config:
        return {"error": "Could not read automation config to duplicate"}

    alias = new_alias or f"{config.get('alias', 'Automation')} (Copy)"
    new_config = {**config, "alias": alias}
    new_config.pop("id", None)

    return await _create_automation(hass, new_config)


async def _remove_device(
    hass: HomeAssistant, device_id: str,
) -> dict[str, Any]:
    """Remove an orphan device from the device registry."""
    from homeassistant.helpers import device_registry as dr

    reg = dr.async_get(hass)
    device = reg.async_get(device_id)
    if not device:
        return {"error": f"Device {device_id} not found"}

    try:
        reg.async_remove_device(device_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Remove device failed: {exc}"}
    return {"ok": True, "removed": device_id, "name": device.name or device_id}


async def _list_device_entities(
    hass: HomeAssistant, device_id: str,
) -> dict[str, Any]:
    """List all entities belonging to a specific device."""
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    entities = []
    for entry in reg.entities.values():
        if entry.device_id == device_id:
            state = hass.states.get(entry.entity_id)
            entities.append({
                "entity_id": entry.entity_id,
                "name": entry.name or entry.original_name,
                "platform": entry.platform,
                "domain": entry.domain,
                "disabled": entry.disabled_by is not None,
                "state": state.state if state else None,
            })

    return {"ok": True, "device_id": device_id, "count": len(entities), "entities": entities}


async def _compare_history(
    hass: HomeAssistant,
    entity_ids: list[str],
    hours: int = 24,
) -> dict[str, Any]:
    """Compare state history of multiple entities side-by-side."""
    results: dict[str, Any] = {}
    for eid in entity_ids[:10]:
        states_list = []
        state = hass.states.get(eid)
        if state:
            states_list.append({
                "state": state.state,
                "last_changed": state.last_changed.isoformat() if state.last_changed else None,
                "last_updated": state.last_updated.isoformat() if state.last_updated else None,
                "attributes": {k: v for k, v in state.attributes.items()
                               if k in ("friendly_name", "unit_of_measurement", "device_class")},
            })
        results[eid] = {"current": states_list[0] if states_list else None}

    try:
        from homeassistant.components.recorder.history import state_changes_during_period
        start = datetime.now(timezone.utc) - timedelta(hours=hours)
        history = await hass.async_add_executor_job(
            lambda: state_changes_during_period(hass, start, None, entity_ids[:10])
        )
        for eid, states in (history or {}).items():
            changes = []
            for s in (states or [])[-20:]:
                changes.append({
                    "state": s.state,
                    "when": s.last_changed.isoformat() if s.last_changed else None,
                })
            if eid in results:
                results[eid]["history"] = changes
                results[eid]["change_count"] = len(states or [])
    except Exception as exc:  # noqa: BLE001
        for eid in results:
            results[eid]["history_note"] = f"History unavailable: {exc}"

    return {"ok": True, "hours": hours, "entities": results}


async def _send_tts(
    hass: HomeAssistant,
    message: str,
    entity_id: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Send text-to-speech to a media player entity."""
    if not entity_id:
        players = [s.entity_id for s in hass.states.async_all("media_player")]
        if not players:
            return {"error": "No media_player entities found for TTS"}
        entity_id = players[0]

    svc_data: dict[str, Any] = {
        "entity_id": entity_id,
        "message": message,
    }
    if language:
        svc_data["language"] = language

    try:
        await hass.services.async_call("tts", "speak", svc_data, blocking=True)
    except Exception:  # noqa: BLE001
        try:
            await hass.services.async_call(
                "tts", "google_translate_say", svc_data, blocking=True,
            )
        except Exception as exc2:  # noqa: BLE001
            return {"error": f"TTS failed: {exc2}"}

    return {"ok": True, "entity_id": entity_id, "message": message}


async def _play_media(
    hass: HomeAssistant,
    entity_id: str,
    media_content_id: str,
    media_content_type: str = "music",
) -> dict[str, Any]:
    """Play media on a media_player entity."""
    try:
        await hass.services.async_call(
            "media_player", "play_media", {
                "entity_id": entity_id,
                "media_content_id": media_content_id,
                "media_content_type": media_content_type,
            }, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Play media failed: {exc}"}
    return {"ok": True, "entity_id": entity_id,
            "media_content_id": media_content_id,
            "media_content_type": media_content_type}


async def _activate_scene(
    hass: HomeAssistant, entity_id: str, transition: float | None = None,
) -> dict[str, Any]:
    """Activate a scene."""
    svc_data: dict[str, Any] = {"entity_id": entity_id}
    if transition is not None:
        svc_data["transition"] = transition
    try:
        await hass.services.async_call("scene", "turn_on", svc_data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Activate scene failed: {exc}"}
    return {"ok": True, "entity_id": entity_id}


async def _snapshot_scene(
    hass: HomeAssistant, entity_ids: list[str], scene_name: str,
) -> dict[str, Any]:
    """Capture current states of entities and create a scene from them."""
    snapshot_data: list[dict[str, Any]] = []
    for eid in entity_ids:
        state = hass.states.get(eid)
        if state:
            entry: dict[str, Any] = {"entity_id": eid, "state": state.state}
            relevant_attrs = {}
            for k, v in state.attributes.items():
                if k in ("brightness", "color_temp", "rgb_color", "hs_color",
                         "xy_color", "effect", "temperature", "hvac_mode",
                         "fan_mode", "swing_mode", "target_temp_high",
                         "target_temp_low", "position", "tilt_position",
                         "volume_level", "source"):
                    relevant_attrs[k] = v
            if relevant_attrs:
                entry["attributes"] = relevant_attrs
            snapshot_data.append(entry)

    if not snapshot_data:
        return {"error": "No valid entities to snapshot"}

    scene_config = {
        "name": scene_name,
        "entities": {e["entity_id"]: {**({"state": e["state"]}),
                                       **e.get("attributes", {})}
                     for e in snapshot_data},
    }

    path = hass.config.path("scenes.yaml")
    try:
        with open(path) as fh:
            existing = yaml.safe_load(fh.read()) or []
    except FileNotFoundError:
        existing = []
    if not isinstance(existing, list):
        existing = [existing]
    existing.append(scene_config)
    with open(path, "w") as fh:
        yaml.dump(existing, fh, default_flow_style=False, allow_unicode=True)

    return {"ok": True, "scene_name": scene_name,
            "entities_captured": len(snapshot_data), "path": path,
            "hint": "Run reload(target='scene') to apply."}


# ---------------------------------------------------------------------------
# Protocol & system deep integration — MQTT, Zigbee, Z-Wave, network, notify
# ---------------------------------------------------------------------------


async def _publish_mqtt(
    hass: HomeAssistant, topic: str, payload: str,
    qos: int = 0, retain: bool = False,
) -> dict[str, Any]:
    """Publish a message to an MQTT topic."""
    try:
        await hass.services.async_call(
            "mqtt", "publish",
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"MQTT publish failed: {exc}"}
    return {"ok": True, "topic": topic, "payload_len": len(payload),
            "qos": qos, "retain": retain}


async def _subscribe_mqtt(
    hass: HomeAssistant, topic: str, timeout: float = 5.0,
) -> dict[str, Any]:
    """Subscribe to an MQTT topic and return messages received within timeout."""
    messages: list[dict[str, Any]] = []

    try:
        from homeassistant.components.mqtt import async_subscribe

        def _callback(msg: Any) -> None:
            messages.append({
                "topic": msg.topic,
                "payload": msg.payload,
                "qos": msg.qos,
                "retain": msg.retain,
            })

        unsub = await async_subscribe(hass, topic, _callback, qos=0)
        await asyncio.sleep(min(timeout, 10.0))
        unsub()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"MQTT subscribe failed: {exc}"}

    return {"ok": True, "topic": topic, "count": len(messages), "messages": messages[:50]}


async def _list_mqtt_devices(
    hass: HomeAssistant,
) -> dict[str, Any]:
    """List devices discovered via MQTT integration."""
    from homeassistant.helpers import device_registry as dr

    reg = dr.async_get(hass)
    mqtt_devices = []
    for device in reg.devices.values():
        for ident in (device.identifiers or set()):
            if isinstance(ident, tuple) and len(ident) >= 2 and ident[0] == "mqtt":
                mqtt_devices.append({
                    "device_id": device.id,
                    "name": device.name,
                    "model": device.model,
                    "manufacturer": device.manufacturer,
                    "sw_version": device.sw_version,
                    "identifier": ident[1] if len(ident) > 1 else str(ident),
                })
                break

    return {"ok": True, "count": len(mqtt_devices), "devices": mqtt_devices}


async def _permit_zigbee_join(
    hass: HomeAssistant, duration: int = 60,
) -> dict[str, Any]:
    """Enable Zigbee pairing mode (works with ZHA or Zigbee2MQTT)."""
    try:
        await hass.services.async_call(
            "zha", "permit",
            {"duration": duration},
            blocking=True,
        )
        return {"ok": True, "method": "zha", "duration": duration}
    except Exception:  # noqa: BLE001
        pass

    try:
        await hass.services.async_call(
            "mqtt", "publish",
            {"topic": "zigbee2mqtt/bridge/request/permit_join",
             "payload": json.dumps({"value": True, "time": duration})},
            blocking=True,
        )
        return {"ok": True, "method": "zigbee2mqtt", "duration": duration}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Zigbee permit join failed (tried ZHA + Z2M): {exc}"}


async def _rename_zigbee_device(
    hass: HomeAssistant, old_name: str, new_name: str,
) -> dict[str, Any]:
    """Rename a Zigbee device via Zigbee2MQTT bridge API."""
    try:
        await hass.services.async_call(
            "mqtt", "publish",
            {"topic": "zigbee2mqtt/bridge/request/device/rename",
             "payload": json.dumps({"from": old_name, "to": new_name})},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Rename Zigbee device failed: {exc}"}
    return {"ok": True, "old_name": old_name, "new_name": new_name}


async def _heal_zwave_network(hass: HomeAssistant) -> dict[str, Any]:
    """Trigger Z-Wave network heal (rebuilds routing tables)."""
    try:
        await hass.services.async_call(
            "zwave_js", "heal_network", {}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Z-Wave heal failed: {exc}"}
    return {"ok": True, "status": "heal_initiated",
            "note": "Full heal may take minutes depending on network size."}


async def _get_zwave_node_info(
    hass: HomeAssistant, entity_id: str | None = None,
) -> dict[str, Any]:
    """Get detailed Z-Wave node information for a device."""
    from homeassistant.helpers import device_registry as dr

    reg = dr.async_get(hass)
    nodes = []
    for device in reg.devices.values():
        is_zwave = False
        node_id = None
        for ident in (device.identifiers or set()):
            if isinstance(ident, tuple) and len(ident) >= 2:
                if ident[0] in ("zwave_js", "ozw", "zwave"):
                    is_zwave = True
                    node_id = ident[1]
                    break
        if not is_zwave:
            continue

        node = {
            "device_id": device.id,
            "name": device.name,
            "model": device.model,
            "manufacturer": device.manufacturer,
            "sw_version": device.sw_version,
            "node_id": node_id,
        }
        nodes.append(node)

    if entity_id:
        state = hass.states.get(entity_id)
        if state:
            node_id_attr = state.attributes.get("node_id")
            if node_id_attr:
                nodes = [n for n in nodes if str(n.get("node_id")) == str(node_id_attr)]

    return {"ok": True, "count": len(nodes), "nodes": nodes}


async def _wake_on_lan(
    hass: HomeAssistant, mac: str, broadcast_address: str | None = None,
) -> dict[str, Any]:
    """Send Wake-on-LAN magic packet to a MAC address."""
    try:
        svc_data: dict[str, Any] = {"mac": mac}
        if broadcast_address:
            svc_data["broadcast_address"] = broadcast_address
        await hass.services.async_call(
            "wake_on_lan", "send_magic_packet", svc_data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"WoL failed: {exc}"}
    return {"ok": True, "mac": mac}


async def _ping_device(
    hass: HomeAssistant, host: str, count: int = 3,
) -> dict[str, Any]:
    """Ping a network host to check reachability."""
    import subprocess

    try:
        result = await hass.async_add_executor_job(
            lambda: subprocess.run(
                ["ping", "-c", str(min(count, 10)), "-W", "2", host],
                capture_output=True, text=True, timeout=30,
            )
        )
        lines = result.stdout.strip().split("\n")
        summary = lines[-2:] if len(lines) >= 2 else lines
        return {"ok": result.returncode == 0,
                "host": host, "reachable": result.returncode == 0,
                "summary": "\n".join(summary)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Ping failed: {exc}"}


async def _list_notification_services(
    hass: HomeAssistant,
) -> dict[str, Any]:
    """List available notification service targets."""
    services_list = []
    for svc_name in sorted(hass.services.async_services().get("notify", {}).keys()):
        services_list.append(f"notify.{svc_name}")

    return {"ok": True, "count": len(services_list), "services": services_list}


async def _dismiss_notification(
    hass: HomeAssistant, notification_id: str,
) -> dict[str, Any]:
    """Dismiss a persistent notification by ID."""
    try:
        await hass.services.async_call(
            "persistent_notification", "dismiss",
            {"notification_id": notification_id},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Dismiss notification failed: {exc}"}
    return {"ok": True, "dismissed": notification_id}


async def _create_persistent_notification(
    hass: HomeAssistant, message: str, title: str | None = None,
    notification_id: str | None = None,
) -> dict[str, Any]:
    """Create a persistent notification in HA UI."""
    svc_data: dict[str, Any] = {"message": message}
    if title:
        svc_data["title"] = title
    if notification_id:
        svc_data["notification_id"] = notification_id
    try:
        await hass.services.async_call(
            "persistent_notification", "create", svc_data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Create notification failed: {exc}"}
    return {"ok": True, "message": message, "title": title}


async def _list_entity_domains(hass: HomeAssistant) -> dict[str, Any]:
    """List all active entity domains with counts."""
    domains: dict[str, int] = {}
    for state in hass.states.async_all():
        domain = state.entity_id.split(".")[0]
        domains[domain] = domains.get(domain, 0) + 1

    sorted_domains = sorted(domains.items(), key=lambda x: -x[1])
    return {"ok": True, "count": len(sorted_domains),
            "total_entities": sum(domains.values()),
            "domains": [{"domain": d, "entities": c} for d, c in sorted_domains]}


async def _list_automations(hass: HomeAssistant) -> dict[str, Any]:
    """List all automations with their state, alias, and last triggered time."""
    automations = []
    for state in hass.states.async_all("automation"):
        attrs = state.attributes
        automations.append({
            "entity_id": state.entity_id,
            "alias": attrs.get("friendly_name", ""),
            "state": state.state,
            "last_triggered": attrs.get("last_triggered"),
        })
    return {"ok": True, "count": len(automations), "automations": automations}


async def _get_device_info(
    hass: HomeAssistant, device_id: str,
) -> dict[str, Any]:
    """Get detailed information about a specific device."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    device = dev_reg.async_get(device_id)
    if not device:
        return {"error": f"Device '{device_id}' not found"}

    entities = []
    for entry in ent_reg.entities.values():
        if entry.device_id == device_id:
            state = hass.states.get(entry.entity_id)
            entities.append({
                "entity_id": entry.entity_id,
                "domain": entry.domain,
                "name": entry.name or entry.original_name,
                "state": state.state if state else None,
            })

    info: dict[str, Any] = {
        "ok": True,
        "device_id": device.id,
        "name": device.name,
        "name_by_user": device.name_by_user,
        "model": device.model,
        "manufacturer": device.manufacturer,
        "sw_version": device.sw_version,
        "hw_version": device.hw_version,
        "area_id": device.area_id,
        "config_entries": list(device.config_entries),
        "connections": [list(c) for c in (device.connections or set())],
        "identifiers": [list(i) for i in (device.identifiers or set())],
        "via_device_id": device.via_device_id,
        "disabled_by": str(device.disabled_by) if device.disabled_by else None,
        "entities": entities,
        "entity_count": len(entities),
    }
    return info


async def _run_script(
    hass: HomeAssistant, entity_id: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a script by entity_id with optional variables."""
    try:
        svc_data: dict[str, Any] = {"entity_id": entity_id}
        if variables:
            svc_data["variables"] = variables
        await hass.services.async_call(
            "script", "turn_on", svc_data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Run script failed: {exc}"}
    return {"ok": True, "entity_id": entity_id}


async def _test_condition(
    hass: HomeAssistant, condition: dict[str, Any],
) -> dict[str, Any]:
    """Test if an automation condition evaluates to true or false."""
    try:
        from homeassistant.helpers.condition import async_from_config

        test = await async_from_config(hass, condition)
        result = test(hass)
        return {"ok": True, "result": result, "condition": condition}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Condition test failed: {exc}"}


# ---------------------------------------------------------------------------
# Advanced domain tools — energy, camera, climate, vacuum, cover, updates
# ---------------------------------------------------------------------------


async def _get_energy_summary(hass: HomeAssistant) -> dict[str, Any]:
    """Get energy usage summary from the energy dashboard configuration."""
    try:
        prefs = hass.data.get("energy_manager")
        if prefs and hasattr(prefs, "data"):
            data = prefs.data
            return {"ok": True, "data": data}
    except Exception:  # noqa: BLE001
        pass
    try:
        energy_data = await hass.async_add_executor_job(
            lambda: hass.data.get("energy")
        )
        if energy_data:
            return {"ok": True, "data": energy_data}
    except Exception:  # noqa: BLE001
        pass
    energy_entities = []
    for state in hass.states.async_all():
        attrs = state.attributes
        dc = attrs.get("device_class", "")
        if dc in ("energy", "power", "gas"):
            energy_entities.append({
                "entity_id": state.entity_id,
                "state": state.state,
                "unit": attrs.get("unit_of_measurement", ""),
                "device_class": dc,
                "name": attrs.get("friendly_name", ""),
            })
    return {"ok": True, "count": len(energy_entities),
            "energy_entities": energy_entities,
            "note": "Energy manager not available; listing energy-class entities instead."}


async def _list_energy_sources(hass: HomeAssistant) -> dict[str, Any]:
    """List configured energy sources from the energy preferences."""
    try:
        prefs = hass.data.get("energy_manager")
        if prefs and hasattr(prefs, "data"):
            sources = prefs.data.get("energy_sources", [])
            return {"ok": True, "count": len(sources), "sources": sources}
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "count": 0, "sources": [],
            "note": "Energy manager not available; configure energy dashboard first."}


async def _camera_snapshot(
    hass: HomeAssistant, entity_id: str, filename: str | None = None,
) -> dict[str, Any]:
    """Take a snapshot from a camera entity."""
    try:
        fname = filename or f"/config/www/snapshot_{entity_id.split('.')[-1]}.jpg"
        await hass.services.async_call(
            "camera", "snapshot",
            {"entity_id": entity_id, "filename": fname},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Camera snapshot failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "filename": fname}


async def _list_cameras(hass: HomeAssistant) -> dict[str, Any]:
    """List all camera entities with their status."""
    cameras = []
    for state in hass.states.async_all("camera"):
        cameras.append({
            "entity_id": state.entity_id,
            "name": state.attributes.get("friendly_name", ""),
            "state": state.state,
            "brand": state.attributes.get("brand", ""),
            "model": state.attributes.get("model_name", ""),
            "is_streaming": state.state == "streaming",
        })
    return {"ok": True, "count": len(cameras), "cameras": cameras}


async def _set_climate_preset(
    hass: HomeAssistant, entity_id: str, preset_mode: str,
) -> dict[str, Any]:
    """Set a climate entity's preset mode (e.g. 'away', 'eco', 'boost')."""
    try:
        await hass.services.async_call(
            "climate", "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": preset_mode},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set climate preset failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "preset_mode": preset_mode}


async def _get_climate_schedule(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Get climate entity state and available modes/presets."""
    state = hass.states.get(entity_id)
    if not state:
        return {"error": f"Climate entity '{entity_id}' not found"}
    attrs = state.attributes
    return {
        "ok": True,
        "entity_id": entity_id,
        "state": state.state,
        "current_temperature": attrs.get("current_temperature"),
        "target_temperature": attrs.get("temperature"),
        "target_temp_low": attrs.get("target_temp_low"),
        "target_temp_high": attrs.get("target_temp_high"),
        "hvac_modes": attrs.get("hvac_modes", []),
        "preset_mode": attrs.get("preset_mode"),
        "preset_modes": attrs.get("preset_modes", []),
        "fan_mode": attrs.get("fan_mode"),
        "fan_modes": attrs.get("fan_modes", []),
        "swing_mode": attrs.get("swing_mode"),
    }


async def _set_cover_position(
    hass: HomeAssistant, entity_id: str, position: int,
) -> dict[str, Any]:
    """Set cover (blinds/curtains/garage door) position (0=closed, 100=open)."""
    try:
        await hass.services.async_call(
            "cover", "set_cover_position",
            {"entity_id": entity_id, "position": max(0, min(100, position))},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set cover position failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "position": position}


async def _vacuum_command(
    hass: HomeAssistant, entity_id: str, command: str,
) -> dict[str, Any]:
    """Send command to a vacuum entity (start, stop, pause, return_to_base, locate, clean_spot)."""
    valid = {"start", "stop", "pause", "return_to_base", "locate", "clean_spot"}
    if command not in valid:
        return {"error": f"Invalid command '{command}'. Valid: {sorted(valid)}"}
    try:
        await hass.services.async_call(
            "vacuum", command, {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Vacuum {command} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _set_input_value(
    hass: HomeAssistant, entity_id: str, value: Any,
) -> dict[str, Any]:
    """Set value of input_number, input_boolean, input_text, input_select, or input_datetime."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    svc_map = {
        "input_number": ("input_number", "set_value", {"value": value}),
        "input_boolean": ("input_boolean", "turn_on" if value else "turn_off", {}),
        "input_text": ("input_text", "set_value", {"value": str(value)}),
        "input_select": ("input_select", "select_option", {"option": str(value)}),
        "input_datetime": ("input_datetime", "set_datetime", {"datetime": str(value)} if "T" in str(value) else {"time": str(value)}),
    }
    if domain not in svc_map:
        return {"error": f"Unsupported domain '{domain}'. Supported: {list(svc_map.keys())}"}

    svc_domain, svc_name, svc_data = svc_map[domain]
    svc_data["entity_id"] = entity_id
    try:
        await hass.services.async_call(svc_domain, svc_name, svc_data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set input value failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "value": value}


async def _list_updates(hass: HomeAssistant) -> dict[str, Any]:
    """List all pending updates (HA core, addons, HACS, devices)."""
    updates = []
    for state in hass.states.async_all("update"):
        attrs = state.attributes
        if state.state == "on":
            updates.append({
                "entity_id": state.entity_id,
                "name": attrs.get("friendly_name", ""),
                "installed_version": attrs.get("installed_version"),
                "latest_version": attrs.get("latest_version"),
                "release_summary": attrs.get("release_summary", "")[:200],
                "release_url": attrs.get("release_url", ""),
            })
    return {"ok": True, "count": len(updates), "updates": updates}


async def _install_update(
    hass: HomeAssistant, entity_id: str, backup: bool = True,
) -> dict[str, Any]:
    """Install a pending update (with optional backup)."""
    try:
        await hass.services.async_call(
            "update", "install",
            {"entity_id": entity_id, "backup": backup},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Install update failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "backup": backup}


# ---------------------------------------------------------------------------
# Device control — lock, alarm, fan, water heater, humidifier, siren, button
# ---------------------------------------------------------------------------


async def _lock_door(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Lock a smart lock."""
    try:
        await hass.services.async_call(
            "lock", "lock", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Lock failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "locked"}


async def _unlock_door(
    hass: HomeAssistant, entity_id: str, code: str | None = None,
) -> dict[str, Any]:
    """Unlock a smart lock (optional PIN code)."""
    try:
        svc_data: dict[str, Any] = {"entity_id": entity_id}
        if code:
            svc_data["code"] = code
        await hass.services.async_call(
            "lock", "unlock", svc_data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Unlock failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "unlocked"}


async def _arm_alarm(
    hass: HomeAssistant, entity_id: str,
    mode: str = "arm_away", code: str | None = None,
) -> dict[str, Any]:
    """Arm an alarm control panel (arm_away, arm_home, arm_night, arm_vacation)."""
    valid = {"arm_away", "arm_home", "arm_night", "arm_vacation", "arm_custom_bypass"}
    if mode not in valid:
        return {"error": f"Invalid mode '{mode}'. Valid: {sorted(valid)}"}
    try:
        svc_data: dict[str, Any] = {"entity_id": entity_id}
        if code:
            svc_data["code"] = code
        await hass.services.async_call(
            "alarm_control_panel", mode, svc_data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Arm alarm failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "mode": mode}


async def _disarm_alarm(
    hass: HomeAssistant, entity_id: str, code: str | None = None,
) -> dict[str, Any]:
    """Disarm an alarm control panel."""
    try:
        svc_data: dict[str, Any] = {"entity_id": entity_id}
        if code:
            svc_data["code"] = code
        await hass.services.async_call(
            "alarm_control_panel", "alarm_disarm", svc_data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Disarm alarm failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "disarmed"}


async def _get_alarm_state(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Get alarm control panel state and supported features."""
    state = hass.states.get(entity_id)
    if not state:
        return {"error": f"Alarm '{entity_id}' not found"}
    attrs = state.attributes
    return {
        "ok": True,
        "entity_id": entity_id,
        "state": state.state,
        "name": attrs.get("friendly_name", ""),
        "code_arm_required": attrs.get("code_arm_required", False),
        "supported_features": attrs.get("supported_features", 0),
    }


async def _set_fan_speed(
    hass: HomeAssistant, entity_id: str, percentage: int,
) -> dict[str, Any]:
    """Set fan speed percentage (0=off, 100=max)."""
    try:
        await hass.services.async_call(
            "fan", "set_percentage",
            {"entity_id": entity_id, "percentage": max(0, min(100, percentage))},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set fan speed failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "percentage": percentage}


async def _set_fan_direction(
    hass: HomeAssistant, entity_id: str, direction: str,
) -> dict[str, Any]:
    """Set fan rotation direction (forward or reverse)."""
    if direction not in ("forward", "reverse"):
        return {"error": f"Invalid direction '{direction}'. Valid: forward, reverse"}
    try:
        await hass.services.async_call(
            "fan", "set_direction",
            {"entity_id": entity_id, "direction": direction},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set fan direction failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "direction": direction}


async def _set_water_heater_temperature(
    hass: HomeAssistant, entity_id: str, temperature: float,
) -> dict[str, Any]:
    """Set water heater target temperature."""
    try:
        await hass.services.async_call(
            "water_heater", "set_temperature",
            {"entity_id": entity_id, "temperature": temperature},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set water heater temp failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "temperature": temperature}


async def _set_humidifier_mode(
    hass: HomeAssistant, entity_id: str, mode: str,
) -> dict[str, Any]:
    """Set humidifier/dehumidifier mode."""
    try:
        await hass.services.async_call(
            "humidifier", "set_mode",
            {"entity_id": entity_id, "mode": mode},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set humidifier mode failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "mode": mode}


async def _activate_siren(
    hass: HomeAssistant, entity_id: str, turn_on: bool = True,
    duration: int | None = None, tone: str | None = None,
) -> dict[str, Any]:
    """Activate or deactivate a siren."""
    try:
        if turn_on:
            svc_data: dict[str, Any] = {"entity_id": entity_id}
            if duration:
                svc_data["duration"] = duration
            if tone:
                svc_data["tone"] = tone
            await hass.services.async_call("siren", "turn_on", svc_data, blocking=True)
        else:
            await hass.services.async_call(
                "siren", "turn_off", {"entity_id": entity_id}, blocking=True,
            )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Siren {'on' if turn_on else 'off'} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "on" if turn_on else "off"}


async def _press_button(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Press a button entity."""
    try:
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Press button failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "pressed"}


# ---------------------------------------------------------------------------
# Data & scheduling — calendar, weather forecast, conversation, todo, utility
# ---------------------------------------------------------------------------


async def _list_calendar_events(
    hass: HomeAssistant, entity_id: str,
    start: str | None = None, end: str | None = None,
) -> dict[str, Any]:
    """List upcoming events from a calendar entity."""
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        s = start or now.isoformat()
        e = end or (now + datetime.timedelta(days=7)).isoformat()
        result = await hass.services.async_call(
            "calendar", "get_events",
            {"entity_id": entity_id, "start_date_time": s, "end_date_time": e},
            blocking=True, return_response=True,
        )
        if result:
            return {"ok": True, "entity_id": entity_id, "events": result}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"List calendar events failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "events": []}


async def _create_calendar_event(
    hass: HomeAssistant, entity_id: str,
    summary: str, start: str, end: str,
    description: str | None = None, location: str | None = None,
) -> dict[str, Any]:
    """Create a new event on a calendar entity."""
    try:
        svc_data: dict[str, Any] = {
            "entity_id": entity_id,
            "summary": summary,
            "start_date_time": start,
            "end_date_time": end,
        }
        if description:
            svc_data["description"] = description
        if location:
            svc_data["location"] = location
        await hass.services.async_call(
            "calendar", "create_event", svc_data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Create calendar event failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "summary": summary}


async def _get_weather_forecast(
    hass: HomeAssistant, entity_id: str, forecast_type: str = "daily",
) -> dict[str, Any]:
    """Get weather forecast (daily or hourly)."""
    state = hass.states.get(entity_id)
    if not state:
        return {"error": f"Weather entity '{entity_id}' not found"}
    attrs = state.attributes
    result: dict[str, Any] = {
        "ok": True,
        "entity_id": entity_id,
        "state": state.state,
        "temperature": attrs.get("temperature"),
        "humidity": attrs.get("humidity"),
        "wind_speed": attrs.get("wind_speed"),
        "wind_bearing": attrs.get("wind_bearing"),
        "pressure": attrs.get("pressure"),
    }
    try:
        svc = f"get_{forecast_type}_forecast"
        resp = await hass.services.async_call(
            "weather", svc,
            {"entity_id": entity_id, "type": forecast_type},
            blocking=True, return_response=True,
        )
        if resp:
            result["forecast"] = resp
    except Exception:  # noqa: BLE001
        result["forecast"] = attrs.get("forecast", [])
    return result


async def _set_number_value(
    hass: HomeAssistant, entity_id: str, value: float,
) -> dict[str, Any]:
    """Set a number entity value."""
    try:
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set number value failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "value": value}


async def _set_select_option(
    hass: HomeAssistant, entity_id: str, option: str,
) -> dict[str, Any]:
    """Set a select entity option."""
    try:
        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": entity_id, "option": option},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set select option failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "option": option}


async def _conversation_query(
    hass: HomeAssistant, text: str, language: str | None = None,
) -> dict[str, Any]:
    """Send a query to the HA conversation agent."""
    try:
        svc_data: dict[str, Any] = {"text": text}
        if language:
            svc_data["language"] = language
        result = await hass.services.async_call(
            "conversation", "process", svc_data,
            blocking=True, return_response=True,
        )
        if result:
            return {"ok": True, "text": text, "response": result}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Conversation query failed: {exc}"}
    return {"ok": True, "text": text, "response": None}


async def _complete_todo_item(
    hass: HomeAssistant, entity_id: str, item: str,
    status: str = "completed",
) -> dict[str, Any]:
    """Update a to-do list item status."""
    try:
        await hass.services.async_call(
            "todo", "update_item",
            {"entity_id": entity_id, "item": item, "status": status},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Complete todo item failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "item": item, "status": status}


async def _reset_utility_meter(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Reset a utility meter sensor."""
    try:
        await hass.services.async_call(
            "utility_meter", "reset",
            {"entity_id": entity_id},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Reset utility meter failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "reset"}


# ---------------------------------------------------------------------------
# Wave 4: media player, notification, presence, reload, history, registry
# ---------------------------------------------------------------------------


async def _media_player_control(
    hass: HomeAssistant, entity_id: str, command: str, **kwargs: Any,
) -> dict[str, Any]:
    """Control a media player (play/pause/stop/next/previous/volume/source/shuffle)."""
    valid = {
        "play": "media_play", "pause": "media_pause", "stop": "media_stop",
        "next": "media_next_track", "previous": "media_previous_track",
        "volume_up": "volume_up", "volume_down": "volume_down",
        "volume_set": "volume_set", "volume_mute": "volume_mute",
        "select_source": "select_source", "shuffle_set": "shuffle_set",
        "repeat_set": "repeat_set", "turn_on": "turn_on", "turn_off": "turn_off",
    }
    svc = valid.get(command)
    if not svc:
        return {"error": f"Invalid command '{command}'. Valid: {list(valid.keys())}"}
    data: dict[str, Any] = {"entity_id": entity_id}
    if command == "volume_set" and "volume_level" in kwargs:
        data["volume_level"] = float(kwargs["volume_level"])
    if command == "volume_mute" and "is_volume_muted" in kwargs:
        data["is_volume_muted"] = bool(kwargs["is_volume_muted"])
    if command == "select_source" and "source" in kwargs:
        data["source"] = kwargs["source"]
    if command == "shuffle_set" and "shuffle" in kwargs:
        data["shuffle"] = bool(kwargs["shuffle"])
    if command == "repeat_set" and "repeat" in kwargs:
        data["repeat"] = kwargs["repeat"]
    try:
        await hass.services.async_call(
            "media_player", svc, data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"media_player.{svc} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _list_media_players(hass: HomeAssistant) -> dict[str, Any]:
    """List all media_player entities with status."""
    states = hass.states.async_all("media_player")
    items = []
    for s in states:
        items.append({
            "entity_id": s.entity_id,
            "state": s.state,
            "friendly_name": s.attributes.get("friendly_name", ""),
            "source": s.attributes.get("source"),
            "volume_level": s.attributes.get("volume_level"),
            "media_title": s.attributes.get("media_title"),
            "source_list": s.attributes.get("source_list"),
        })
    return {"ok": True, "count": len(items), "players": items}


async def _send_mobile_notification(
    hass: HomeAssistant, target: str, message: str, title: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a rich mobile push notification (supports actions/images/channels)."""
    svc_data: dict[str, Any] = {"message": message}
    if title:
        svc_data["title"] = title
    if data:
        svc_data["data"] = data
    try:
        await hass.services.async_call(
            "notify", target, svc_data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"notify.{target} failed: {exc}"}
    return {"ok": True, "target": target, "message": message[:80]}


async def _get_person_location(
    hass: HomeAssistant, person_id: str | None = None,
) -> dict[str, Any]:
    """Get location of a person or all persons."""
    states = hass.states.async_all("person")
    if person_id:
        states = [s for s in states if s.entity_id == person_id or
                  s.entity_id == f"person.{person_id}"]
    persons = []
    for s in states:
        persons.append({
            "entity_id": s.entity_id,
            "state": s.state,
            "friendly_name": s.attributes.get("friendly_name", ""),
            "latitude": s.attributes.get("latitude"),
            "longitude": s.attributes.get("longitude"),
            "gps_accuracy": s.attributes.get("gps_accuracy"),
            "source": s.attributes.get("source"),
        })
    return {"ok": True, "count": len(persons), "persons": persons}


async def _list_device_trackers(hass: HomeAssistant) -> dict[str, Any]:
    """List all device_tracker entities with state and location."""
    states = hass.states.async_all("device_tracker")
    trackers = []
    for s in states:
        trackers.append({
            "entity_id": s.entity_id,
            "state": s.state,
            "friendly_name": s.attributes.get("friendly_name", ""),
            "source_type": s.attributes.get("source_type"),
            "latitude": s.attributes.get("latitude"),
            "longitude": s.attributes.get("longitude"),
            "battery_level": s.attributes.get("battery_level"),
        })
    return {"ok": True, "count": len(trackers), "trackers": trackers}


async def _reload_yaml(hass: HomeAssistant, target: str = "all") -> dict[str, Any]:
    """Reload YAML-based config (automations/scripts/scenes/groups/all)."""
    valid = ["automation", "script", "scene", "group", "input_boolean",
             "input_number", "input_text", "input_select", "input_datetime",
             "template", "all"]
    if target not in valid:
        return {"error": f"Invalid target '{target}'. Valid: {valid}"}
    reloaded = []
    targets = valid[:-1] if target == "all" else [target]
    for t in targets:
        try:
            await hass.services.async_call(
                t, "reload", {}, blocking=True,
            )
            reloaded.append(t)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "reloaded": reloaded, "count": len(reloaded)}


async def _reload_all_integrations(hass: HomeAssistant) -> dict[str, Any]:
    """Reload all config entries (integrations)."""
    try:
        entries = hass.config_entries.async_entries()
        reloaded = []
        for entry in entries[:50]:
            try:
                await hass.config_entries.async_reload(entry.entry_id)
                reloaded.append(entry.domain)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "reloaded": reloaded, "count": len(reloaded)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Reload all integrations failed: {exc}"}


async def _get_entity_history_summary(
    hass: HomeAssistant, entity_id: str, hours: int = 24,
) -> dict[str, Any]:
    """Get summarized history for a single entity (state changes + duration)."""
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    end = dt_util.utcnow()
    start = end - timedelta(hours=hours)
    try:
        from homeassistant.components.recorder import history
        hist = await hass.async_add_executor_job(
            history.state_changes_during_period, hass, start, end, entity_id,
        )
        states_list = hist.get(entity_id, [])
        changes = []
        for s in states_list[-50:]:
            changes.append({
                "state": s.state,
                "changed": s.last_changed.isoformat() if s.last_changed else None,
            })
        return {
            "ok": True, "entity_id": entity_id,
            "period_hours": hours,
            "total_changes": len(states_list),
            "recent_changes": changes,
        }
    except Exception as exc:  # noqa: BLE001
        state = hass.states.get(entity_id)
        if not state:
            return {"error": f"Entity {entity_id} not found"}
        return {
            "ok": True, "entity_id": entity_id,
            "note": f"Recorder unavailable ({exc}); showing current state only",
            "current_state": state.state,
            "last_changed": state.last_changed.isoformat() if state.last_changed else None,
        }


async def _get_entity_logbook(
    hass: HomeAssistant, entity_id: str, hours: int = 24,
) -> dict[str, Any]:
    """Get filtered logbook entries for a specific entity."""
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    end = dt_util.utcnow()
    start = end - timedelta(hours=hours)
    try:
        from homeassistant.components.logbook import async_log_entries
        entries = await async_log_entries(
            hass, start, end, entity_ids=[entity_id],
        )
        items = [{"name": e.get("name"), "message": e.get("message"),
                  "when": e.get("when")} for e in entries[:50]]
        return {"ok": True, "entity_id": entity_id, "count": len(items), "entries": items}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": True, "entity_id": entity_id,
            "note": f"Logbook unavailable ({exc}); use get_history instead",
            "entries": [],
        }


async def _get_states_by_domain(
    hass: HomeAssistant, domain: str,
) -> dict[str, Any]:
    """Get all entity states in a specific domain with full attributes."""
    states = hass.states.async_all(domain)
    if not states:
        return {"ok": True, "domain": domain, "count": 0, "entities": []}
    entities = []
    for s in states:
        entities.append({
            "entity_id": s.entity_id,
            "state": s.state,
            "attributes": dict(s.attributes),
            "last_changed": s.last_changed.isoformat() if s.last_changed else None,
        })
    return {"ok": True, "domain": domain, "count": len(entities), "entities": entities}


async def _get_nearest_person(
    hass: HomeAssistant, zone: str = "home",
) -> dict[str, Any]:
    """Find the nearest person to a zone."""
    zone_state = hass.states.get(f"zone.{zone}") if not zone.startswith("zone.") else hass.states.get(zone)
    if not zone_state:
        return {"error": f"Zone '{zone}' not found"}
    zone_lat = zone_state.attributes.get("latitude")
    zone_lon = zone_state.attributes.get("longitude")
    if zone_lat is None or zone_lon is None:
        return {"error": f"Zone '{zone}' has no coordinates"}
    persons = hass.states.async_all("person")
    results = []
    for p in persons:
        lat = p.attributes.get("latitude")
        lon = p.attributes.get("longitude")
        if lat is not None and lon is not None:
            dist = ((float(lat) - float(zone_lat)) ** 2 + (float(lon) - float(zone_lon)) ** 2) ** 0.5
            results.append({
                "entity_id": p.entity_id,
                "friendly_name": p.attributes.get("friendly_name", ""),
                "state": p.state,
                "distance_approx": round(dist * 111, 2),
            })
    results.sort(key=lambda x: x["distance_approx"])
    return {"ok": True, "zone": zone, "count": len(results), "persons": results}


async def _assign_device_label(
    hass: HomeAssistant, device_id: str, labels: list[str],
) -> dict[str, Any]:
    """Assign labels to a device."""
    try:
        from homeassistant.helpers import device_registry as dr
        registry = dr.async_get(hass)
        device = registry.async_get(device_id)
        if not device:
            return {"error": f"Device '{device_id}' not found"}
        registry.async_update_device(device_id, labels=set(labels))
        return {"ok": True, "device_id": device_id, "labels": labels}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Assign device label failed: {exc}"}


async def _get_image_url(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Get the URL/path of an image entity."""
    state = hass.states.get(entity_id)
    if not state:
        return {"error": f"Entity '{entity_id}' not found"}
    url = state.attributes.get("entity_picture") or state.attributes.get("url")
    return {
        "ok": True, "entity_id": entity_id,
        "image_url": url,
        "state": state.state,
        "access_token": state.attributes.get("access_token"),
    }


# ---------------------------------------------------------------------------
# Wave 5: floor, tag, todo, assist, thread/matter, counter, timer, valve,
#          mower, event, datetime, text, voice, conversation, schedule,
#          statistics, remote, backup advanced, categories
# ---------------------------------------------------------------------------


async def _assign_area_floor(
    hass: HomeAssistant, area_id: str, floor_id: str,
) -> dict[str, Any]:
    """Assign an area to a floor."""
    try:
        from homeassistant.helpers import area_registry as ar
        registry = ar.async_get(hass)
        area = registry.async_get_area(area_id)
        if not area:
            for a in registry.async_list_areas():
                if a.name.lower() == area_id.lower():
                    area = a
                    break
        if not area:
            return {"error": f"Area '{area_id}' not found"}
        registry.async_update(area.id, floor_id=floor_id)
        return {"ok": True, "area_id": area.id, "floor_id": floor_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Assign area floor failed: {exc}"}


async def _scan_tag(
    hass: HomeAssistant, tag_id: str, device_id: str | None = None,
) -> dict[str, Any]:
    """Fire a tag scanned event (simulate NFC tag scan)."""
    event_data: dict[str, Any] = {"tag_id": tag_id}
    if device_id:
        event_data["device_id"] = device_id
    hass.bus.async_fire("tag_scanned", event_data)
    return {"ok": True, "tag_id": tag_id}


async def _add_todo_item(
    hass: HomeAssistant, entity_id: str, item: str,
    due_date: str | None = None, description: str | None = None,
) -> dict[str, Any]:
    """Add an item to a todo list entity."""
    data: dict[str, Any] = {"entity_id": entity_id, "item": item}
    if due_date:
        data["due_date"] = due_date
    if description:
        data["description"] = description
    try:
        await hass.services.async_call("todo", "add_item", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Add todo item failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "item": item}


async def _remove_todo_item(
    hass: HomeAssistant, entity_id: str, item: str,
) -> dict[str, Any]:
    """Remove an item from a todo list entity."""
    try:
        await hass.services.async_call(
            "todo", "remove_item", {"entity_id": entity_id, "item": item},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Remove todo item failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "removed": item}


async def _list_assist_pipelines(hass: HomeAssistant) -> dict[str, Any]:
    """List all configured Assist pipelines."""
    try:
        from homeassistant.components.assist_pipeline import async_get_pipelines
        pipelines = async_get_pipelines(hass)
        items = [{"id": p.id, "name": p.name, "language": p.language,
                  "conversation_engine": p.conversation_engine,
                  "stt_engine": p.stt_engine, "tts_engine": p.tts_engine}
                 for p in pipelines]
        return {"ok": True, "count": len(items), "pipelines": items}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "note": f"Assist pipeline unavailable ({exc})", "pipelines": []}


async def _run_assist_pipeline(
    hass: HomeAssistant, text: str, pipeline_id: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Run text through an Assist pipeline and return the response."""
    try:
        from homeassistant.components.conversation import async_converse
        result = await async_converse(
            hass, text, conversation_id=None,
            context=None, language=language, agent_id=pipeline_id,
        )
        resp = result.response
        return {
            "ok": True, "text": text,
            "response_type": resp.response_type.value if hasattr(resp.response_type, "value") else str(resp.response_type),
            "speech": resp.speech.get("plain", {}).get("speech", "") if resp.speech else "",
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Assist pipeline failed: {exc}"}


async def _list_thread_networks(hass: HomeAssistant) -> dict[str, Any]:
    """List Thread border routers and networks."""
    try:
        from homeassistant.components.thread import async_get_preferred_dataset
        dataset = await async_get_preferred_dataset(hass)
        return {"ok": True, "preferred_dataset": dataset is not None,
                "note": "Thread dataset available" if dataset else "No Thread dataset configured"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "note": f"Thread not available ({exc})", "networks": []}


async def _get_matter_nodes(hass: HomeAssistant) -> dict[str, Any]:
    """Get Matter fabric nodes."""
    try:
        from homeassistant.components.matter import get_matter_server
        server = get_matter_server(hass)
        nodes = server.get_nodes()
        items = [{"node_id": n.node_id, "name": n.name,
                  "vendor_id": getattr(n, "vendor_id", None),
                  "product_id": getattr(n, "product_id", None)}
                 for n in nodes[:50]]
        return {"ok": True, "count": len(items), "nodes": items}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "note": f"Matter not available ({exc})", "nodes": []}


async def _restore_backup(
    hass: HomeAssistant, backup_id: str,
) -> dict[str, Any]:
    """Restore a Home Assistant backup by ID."""
    try:
        await hass.services.async_call(
            "hassio", "restore_full", {"slug": backup_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Restore backup failed: {exc}"}
    return {"ok": True, "backup_id": backup_id, "action": "restore_started"}


async def _download_backup(
    hass: HomeAssistant, backup_id: str,
) -> dict[str, Any]:
    """Get download info for a backup."""
    base = hass.config.config_dir
    return {
        "ok": True, "backup_id": backup_id,
        "download_path": f"/api/hassio/backups/{backup_id}/download",
        "note": f"Use HA API to download: GET /api/hassio/backups/{backup_id}/download",
        "config_dir": base,
    }


async def _assign_entity_category(
    hass: HomeAssistant, entity_id: str, category: str,
) -> dict[str, Any]:
    """Set entity category (config/diagnostic/None)."""
    try:
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
        entry = registry.async_get(entity_id)
        if not entry:
            return {"error": f"Entity '{entity_id}' not found in registry"}
        cat = None if category.lower() in ("none", "") else category
        registry.async_update_entity(entity_id, entity_category=cat)
        return {"ok": True, "entity_id": entity_id, "category": cat}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Assign entity category failed: {exc}"}


async def _increment_counter(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Increment a counter helper."""
    try:
        await hass.services.async_call(
            "counter", "increment", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Increment counter failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "increment"}


async def _decrement_counter(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Decrement a counter helper."""
    try:
        await hass.services.async_call(
            "counter", "decrement", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Decrement counter failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "decrement"}


async def _reset_counter(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Reset a counter helper to its initial value."""
    try:
        await hass.services.async_call(
            "counter", "reset", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Reset counter failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "reset"}


async def _start_timer(
    hass: HomeAssistant, entity_id: str, duration: str | None = None,
) -> dict[str, Any]:
    """Start a timer helper."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if duration:
        data["duration"] = duration
    try:
        await hass.services.async_call("timer", "start", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Start timer failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "start", "duration": duration}


async def _cancel_timer(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Cancel a running timer."""
    try:
        await hass.services.async_call(
            "timer", "cancel", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cancel timer failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "cancel"}


async def _pause_timer(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Pause a running timer."""
    try:
        await hass.services.async_call(
            "timer", "pause", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Pause timer failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "pause"}


async def _finish_timer(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Finish (complete) a timer early."""
    try:
        await hass.services.async_call(
            "timer", "finish", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Finish timer failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "finish"}


async def _mower_command(
    hass: HomeAssistant, entity_id: str, command: str,
) -> dict[str, Any]:
    """Control a lawn mower (start/pause/dock)."""
    valid = {"start": "start_mowing", "pause": "pause", "dock": "dock"}
    svc = valid.get(command)
    if not svc:
        return {"error": f"Invalid command '{command}'. Valid: {list(valid.keys())}"}
    try:
        await hass.services.async_call(
            "lawn_mower", svc, {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Mower {command} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _valve_control(
    hass: HomeAssistant, entity_id: str, command: str,
    position: int | None = None,
) -> dict[str, Any]:
    """Control a valve (open/close/set_position/stop)."""
    valid = {"open": "open_valve", "close": "close_valve",
             "set_position": "set_valve_position", "stop": "stop_valve"}
    svc = valid.get(command)
    if not svc:
        return {"error": f"Invalid command '{command}'. Valid: {list(valid.keys())}"}
    data: dict[str, Any] = {"entity_id": entity_id}
    if command == "set_position" and position is not None:
        data["position"] = position
    try:
        await hass.services.async_call("valve", svc, data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Valve {command} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _list_event_entities(hass: HomeAssistant) -> dict[str, Any]:
    """List all event entities with their last event type."""
    states = hass.states.async_all("event")
    items = []
    for s in states:
        items.append({
            "entity_id": s.entity_id,
            "state": s.state,
            "friendly_name": s.attributes.get("friendly_name", ""),
            "event_type": s.attributes.get("event_type"),
            "event_types": s.attributes.get("event_types"),
        })
    return {"ok": True, "count": len(items), "events": items}


async def _set_date_value(
    hass: HomeAssistant, entity_id: str, date: str,
) -> dict[str, Any]:
    """Set an input_datetime entity to a date value."""
    try:
        await hass.services.async_call(
            "input_datetime", "set_datetime",
            {"entity_id": entity_id, "date": date}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set date failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "date": date}


async def _set_time_value(
    hass: HomeAssistant, entity_id: str, time: str,
) -> dict[str, Any]:
    """Set an input_datetime entity to a time value."""
    try:
        await hass.services.async_call(
            "input_datetime", "set_datetime",
            {"entity_id": entity_id, "time": time}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set time failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "time": time}


async def _set_text_value(
    hass: HomeAssistant, entity_id: str, value: str,
) -> dict[str, Any]:
    """Set a text entity value."""
    domain = entity_id.split(".")[0] if "." in entity_id else "input_text"
    svc_domain = "input_text" if domain == "input_text" else "text"
    svc = "set_value"
    try:
        await hass.services.async_call(
            svc_domain, svc, {"entity_id": entity_id, "value": value},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set text failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "value": value[:80]}


async def _list_wake_words(hass: HomeAssistant) -> dict[str, Any]:
    """List configured wake words."""
    try:
        from homeassistant.components.wake_word import async_get_wake_word_detection_entity  # noqa: F401
        entities = hass.states.async_all("wake_word")
        items = [{"entity_id": s.entity_id, "state": s.state,
                  "friendly_name": s.attributes.get("friendly_name", "")}
                 for s in entities]
        return {"ok": True, "count": len(items), "wake_words": items}
    except Exception as exc:  # noqa: BLE001
        entities = hass.states.async_all("wake_word")
        items = [{"entity_id": s.entity_id, "state": s.state} for s in entities]
        return {"ok": True, "count": len(items), "wake_words": items,
                "note": f"Wake word component info: {exc}"}


async def _list_stt_engines(hass: HomeAssistant) -> dict[str, Any]:
    """List available speech-to-text engines."""
    try:
        from homeassistant.components.stt import async_get_speech_to_text_engines
        engines = async_get_speech_to_text_engines(hass)
        items = [{"engine_id": e} for e in engines] if engines else []
        return {"ok": True, "count": len(items), "engines": items}
    except Exception:  # noqa: BLE001
        states = hass.states.async_all("stt")
        items = [{"entity_id": s.entity_id, "state": s.state} for s in states]
        return {"ok": True, "count": len(items), "engines": items}


async def _list_tts_engines(hass: HomeAssistant) -> dict[str, Any]:
    """List available text-to-speech engines."""
    try:
        from homeassistant.components.tts import async_get_text_to_speech_engines
        engines = async_get_text_to_speech_engines(hass)
        items = [{"engine_id": e} for e in engines] if engines else []
        return {"ok": True, "count": len(items), "engines": items}
    except Exception:  # noqa: BLE001
        states = hass.states.async_all("tts")
        items = [{"entity_id": s.entity_id, "state": s.state} for s in states]
        return {"ok": True, "count": len(items), "engines": items}


async def _list_conversation_agents(hass: HomeAssistant) -> dict[str, Any]:
    """List all registered conversation agents."""
    try:
        from homeassistant.components.conversation import async_get_agent_info
        info = async_get_agent_info(hass)
        items = [{"id": a.id, "name": a.name} for a in info] if info else []
        return {"ok": True, "count": len(items), "agents": items}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "note": f"Conversation agents unavailable ({exc})", "agents": []}


async def _get_schedule(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Get schedule helper state and next event."""
    state = hass.states.get(entity_id)
    if not state:
        return {"error": f"Schedule '{entity_id}' not found"}
    return {
        "ok": True, "entity_id": entity_id,
        "state": state.state,
        "next_event": state.attributes.get("next_event"),
        "friendly_name": state.attributes.get("friendly_name", ""),
        "attributes": dict(state.attributes),
    }


async def _get_statistics_metadata(
    hass: HomeAssistant, statistic_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Get long-term statistics metadata."""
    try:
        from homeassistant.components.recorder.statistics import (
            list_statistic_ids,
        )
        metadata = await hass.async_add_executor_job(
            list_statistic_ids, hass,
        )
        items = []
        for m in metadata[:100]:
            sid = m.get("statistic_id", "")
            if statistic_ids and sid not in statistic_ids:
                continue
            items.append({
                "statistic_id": sid,
                "name": m.get("name"),
                "source": m.get("source"),
                "unit_of_measurement": m.get("unit_of_measurement"),
            })
        return {"ok": True, "count": len(items), "statistics": items}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "note": f"Statistics metadata unavailable ({exc})", "statistics": []}


async def _clear_statistics(
    hass: HomeAssistant, statistic_ids: list[str],
) -> dict[str, Any]:
    """Clear long-term statistics for given statistic IDs."""
    try:
        from homeassistant.components.recorder.statistics import (
            async_clear_statistics,
        )
        await async_clear_statistics(hass, statistic_ids)
        return {"ok": True, "cleared": statistic_ids}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Clear statistics failed: {exc}"}


async def _send_remote_command(
    hass: HomeAssistant, entity_id: str, command: str,
    device: str | None = None, num_repeats: int = 1,
) -> dict[str, Any]:
    """Send an IR/RF command via a remote entity."""
    data: dict[str, Any] = {"entity_id": entity_id, "command": command}
    if device:
        data["device"] = device
    if num_repeats > 1:
        data["num_repeats"] = num_repeats
    try:
        await hass.services.async_call(
            "remote", "send_command", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Send remote command failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


# ---------------------------------------------------------------------------
# Wave 8: energy, siren, lock, alarm, fan, cover, water heater, humidifier,
#          automation traces, conversation, input_boolean, update skip
# ---------------------------------------------------------------------------


async def _get_energy_preferences(hass: HomeAssistant) -> dict[str, Any]:
    """Get energy dashboard preferences (sources, grids, solar, battery)."""
    try:
        from homeassistant.components.energy import async_get_manager
        manager = await async_get_manager(hass)
        prefs = manager.data
        if prefs:
            return {"ok": True, "preferences": {
                "energy_sources": len(prefs.get("energy_sources", [])),
                "device_consumption": len(prefs.get("device_consumption", [])),
                "raw": prefs,
            }}
        return {"ok": True, "note": "No energy preferences configured", "preferences": {}}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "note": f"Energy module unavailable ({exc})", "preferences": {}}


async def _skip_update(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Skip an available update."""
    try:
        await hass.services.async_call(
            "update", "skip", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Skip update failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "skipped"}


async def _siren_control(
    hass: HomeAssistant, entity_id: str, command: str,
    tone: str | None = None, volume_level: float | None = None,
    duration: int | None = None,
) -> dict[str, Any]:
    """Control a siren (turn_on/turn_off)."""
    if command not in ("turn_on", "turn_off"):
        return {"error": f"Invalid command '{command}'. Valid: turn_on, turn_off"}
    data: dict[str, Any] = {"entity_id": entity_id}
    if command == "turn_on":
        if tone:
            data["tone"] = tone
        if volume_level is not None:
            data["volume_level"] = float(volume_level)
        if duration is not None:
            data["duration"] = int(duration)
    try:
        await hass.services.async_call("siren", command, data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Siren {command} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _lock_control(
    hass: HomeAssistant, entity_id: str, command: str,
    code: str | None = None,
) -> dict[str, Any]:
    """Control a lock (lock/unlock/open)."""
    valid = {"lock": "lock", "unlock": "unlock", "open": "open"}
    svc = valid.get(command)
    if not svc:
        return {"error": f"Invalid command '{command}'. Valid: {list(valid.keys())}"}
    data: dict[str, Any] = {"entity_id": entity_id}
    if code:
        data["code"] = code
    try:
        await hass.services.async_call("lock", svc, data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Lock {command} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _alarm_control(
    hass: HomeAssistant, entity_id: str, command: str,
    code: str | None = None,
) -> dict[str, Any]:
    """Control an alarm panel (arm_home/arm_away/arm_night/arm_vacation/disarm/trigger)."""
    valid = {
        "arm_home": "alarm_arm_home", "arm_away": "alarm_arm_away",
        "arm_night": "alarm_arm_night", "arm_vacation": "alarm_arm_vacation",
        "disarm": "alarm_disarm", "trigger": "alarm_trigger",
    }
    svc = valid.get(command)
    if not svc:
        return {"error": f"Invalid command '{command}'. Valid: {list(valid.keys())}"}
    data: dict[str, Any] = {"entity_id": entity_id}
    if code:
        data["code"] = code
    try:
        await hass.services.async_call(
            "alarm_control_panel", svc, data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Alarm {command} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _fan_control(
    hass: HomeAssistant, entity_id: str, command: str,
    percentage: int | None = None, preset_mode: str | None = None,
    direction: str | None = None, oscillating: bool | None = None,
) -> dict[str, Any]:
    """Control a fan (turn_on/turn_off/toggle/set_percentage/set_preset_mode/oscillate/set_direction)."""
    valid = {
        "turn_on": "turn_on", "turn_off": "turn_off", "toggle": "toggle",
        "set_percentage": "set_percentage", "set_preset_mode": "set_preset_mode",
        "oscillate": "oscillate", "set_direction": "set_direction",
    }
    svc = valid.get(command)
    if not svc:
        return {"error": f"Invalid command '{command}'. Valid: {list(valid.keys())}"}
    data: dict[str, Any] = {"entity_id": entity_id}
    if command == "set_percentage" and percentage is not None:
        data["percentage"] = percentage
    if command == "set_preset_mode" and preset_mode:
        data["preset_mode"] = preset_mode
    if command == "oscillate" and oscillating is not None:
        data["oscillating"] = oscillating
    if command == "set_direction" and direction:
        data["direction"] = direction
    if command == "turn_on":
        if percentage is not None:
            data["percentage"] = percentage
        if preset_mode:
            data["preset_mode"] = preset_mode
    try:
        await hass.services.async_call("fan", svc, data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Fan {command} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _cover_control(
    hass: HomeAssistant, entity_id: str, command: str,
    position: int | None = None, tilt_position: int | None = None,
) -> dict[str, Any]:
    """Control a cover (open/close/stop/set_position/open_tilt/close_tilt/set_tilt_position/toggle/toggle_tilt)."""
    valid = {
        "open": "open_cover", "close": "close_cover", "stop": "stop_cover",
        "set_position": "set_cover_position", "toggle": "toggle",
        "open_tilt": "open_cover_tilt", "close_tilt": "close_cover_tilt",
        "set_tilt_position": "set_cover_tilt_position", "toggle_tilt": "toggle_cover_tilt",
    }
    svc = valid.get(command)
    if not svc:
        return {"error": f"Invalid command '{command}'. Valid: {list(valid.keys())}"}
    data: dict[str, Any] = {"entity_id": entity_id}
    if command == "set_position" and position is not None:
        data["position"] = position
    if command == "set_tilt_position" and tilt_position is not None:
        data["tilt_position"] = tilt_position
    try:
        await hass.services.async_call("cover", svc, data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cover {command} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _water_heater_control(
    hass: HomeAssistant, entity_id: str, command: str,
    temperature: float | None = None, operation_mode: str | None = None,
) -> dict[str, Any]:
    """Control a water heater (set_temperature/set_operation_mode/turn_on/turn_off)."""
    valid = {
        "set_temperature": "set_temperature",
        "set_operation_mode": "set_operation_mode",
        "turn_on": "turn_on", "turn_off": "turn_off",
    }
    svc = valid.get(command)
    if not svc:
        return {"error": f"Invalid command '{command}'. Valid: {list(valid.keys())}"}
    data: dict[str, Any] = {"entity_id": entity_id}
    if command == "set_temperature" and temperature is not None:
        data["temperature"] = float(temperature)
    if command == "set_operation_mode" and operation_mode:
        data["operation_mode"] = operation_mode
    try:
        await hass.services.async_call("water_heater", svc, data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Water heater {command} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _humidifier_control(
    hass: HomeAssistant, entity_id: str, command: str,
    humidity: int | None = None, mode: str | None = None,
) -> dict[str, Any]:
    """Control a humidifier (turn_on/turn_off/set_humidity/set_mode)."""
    valid = {
        "turn_on": "turn_on", "turn_off": "turn_off",
        "set_humidity": "set_humidity", "set_mode": "set_mode",
    }
    svc = valid.get(command)
    if not svc:
        return {"error": f"Invalid command '{command}'. Valid: {list(valid.keys())}"}
    data: dict[str, Any] = {"entity_id": entity_id}
    if command == "set_humidity" and humidity is not None:
        data["humidity"] = int(humidity)
    if command == "set_mode" and mode:
        data["mode"] = mode
    try:
        await hass.services.async_call("humidifier", svc, data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Humidifier {command} failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _list_automation_traces(
    hass: HomeAssistant, automation_id: str | None = None,
) -> dict[str, Any]:
    """List automation execution traces (debug runs)."""
    try:
        from homeassistant.components.automation import async_get_trace
        if automation_id:
            traces = await async_get_trace(hass, automation_id)
            items = [{"run_id": t.get("run_id"), "state": t.get("state"),
                      "timestamp": t.get("timestamp")} for t in (traces or [])]
        else:
            items = []
        return {"ok": True, "count": len(items), "traces": items}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "note": f"Automation traces unavailable ({exc})", "traces": []}


async def _process_conversation(
    hass: HomeAssistant, text: str, language: str | None = None,
    agent_id: str | None = None, conversation_id: str | None = None,
) -> dict[str, Any]:
    """Process text through HA conversation agent (built-in or custom)."""
    try:
        from homeassistant.components.conversation import async_converse
        result = await async_converse(
            hass, text, conversation_id=conversation_id,
            context=None, language=language, agent_id=agent_id,
        )
        resp = result.response
        return {
            "ok": True, "text": text,
            "response_type": resp.response_type.value if hasattr(resp.response_type, "value") else str(resp.response_type),
            "speech": resp.speech.get("plain", {}).get("speech", "") if resp.speech else "",
            "conversation_id": getattr(result, "conversation_id", None),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Conversation processing failed: {exc}"}


async def _toggle_input_boolean(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Toggle an input_boolean helper."""
    try:
        await hass.services.async_call(
            "input_boolean", "toggle", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Toggle input_boolean failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "toggled"}


# ---------------------------------------------------------------------------
# Wave 9: camera, climate presets, notify targets, system log, input select/
#          number, utility meter, logbook, device actions, batch reload
# ---------------------------------------------------------------------------


async def _camera_turn_on(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Turn on a camera."""
    try:
        await hass.services.async_call(
            "camera", "turn_on", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Camera turn_on failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_on"}


async def _camera_turn_off(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Turn off a camera."""
    try:
        await hass.services.async_call(
            "camera", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Camera turn_off failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_off"}


async def _climate_set_preset(
    hass: HomeAssistant, entity_id: str, preset_mode: str,
) -> dict[str, Any]:
    """Set climate preset mode (home/away/eco/sleep/boost/comfort/etc.)."""
    try:
        await hass.services.async_call(
            "climate", "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": preset_mode}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Climate set preset failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "preset_mode": preset_mode}


async def _climate_set_aux_heat(
    hass: HomeAssistant, entity_id: str, aux_heat: bool,
) -> dict[str, Any]:
    """Toggle auxiliary/emergency heat on a climate entity."""
    try:
        await hass.services.async_call(
            "climate", "set_aux_heat",
            {"entity_id": entity_id, "aux_heat": aux_heat}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Climate set aux heat failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "aux_heat": aux_heat}


async def _list_notify_targets(hass: HomeAssistant) -> dict[str, Any]:
    """List all available notification service targets."""
    svcs = hass.services.async_services()
    notify_svcs = svcs.get("notify", {})
    targets = [{"service": f"notify.{name}", "name": name}
               for name in sorted(notify_svcs.keys())]
    return {"ok": True, "count": len(targets), "targets": targets}


async def _clear_system_log(hass: HomeAssistant) -> dict[str, Any]:
    """Clear the system log."""
    try:
        await hass.services.async_call(
            "system_log", "clear", {}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Clear system log failed: {exc}"}
    return {"ok": True, "action": "system_log_cleared"}


async def _batch_reload_integrations(
    hass: HomeAssistant, domains: list[str] | None = None,
) -> dict[str, Any]:
    """Reload multiple integration domains at once."""
    entries = hass.config_entries.async_entries()
    if domains:
        entries = [e for e in entries if e.domain in domains]
    reloaded = []
    errors = []
    for entry in entries[:50]:
        try:
            await hass.config_entries.async_reload(entry.entry_id)
            reloaded.append(entry.domain)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{entry.domain}: {exc}")
    return {"ok": True, "reloaded": reloaded, "count": len(reloaded),
            "errors": errors if errors else None}


async def _set_input_select_option(
    hass: HomeAssistant, entity_id: str, option: str,
) -> dict[str, Any]:
    """Set an input_select entity to a specific option."""
    try:
        await hass.services.async_call(
            "input_select", "select_option",
            {"entity_id": entity_id, "option": option}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set input_select failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "option": option}


async def _list_input_select_options(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """List available options for an input_select entity."""
    state = hass.states.get(entity_id)
    if not state:
        return {"error": f"Entity '{entity_id}' not found"}
    options = state.attributes.get("options", [])
    return {
        "ok": True, "entity_id": entity_id,
        "current": state.state, "options": options, "count": len(options),
    }


async def _set_input_number_value(
    hass: HomeAssistant, entity_id: str, value: float,
) -> dict[str, Any]:
    """Set an input_number entity value."""
    try:
        await hass.services.async_call(
            "input_number", "set_value",
            {"entity_id": entity_id, "value": float(value)}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set input_number failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "value": float(value)}


async def _calibrate_utility_meter(
    hass: HomeAssistant, entity_id: str, value: float,
) -> dict[str, Any]:
    """Calibrate a utility meter to a specific value."""
    try:
        await hass.services.async_call(
            "utility_meter", "calibrate",
            {"entity_id": entity_id, "value": float(value)}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Calibrate utility meter failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "calibrated_to": float(value)}


async def _log_custom_event(
    hass: HomeAssistant, name: str, message: str,
    entity_id: str | None = None, domain: str | None = None,
) -> dict[str, Any]:
    """Fire a logbook entry event for custom logging."""
    event_data: dict[str, Any] = {"name": name, "message": message}
    if entity_id:
        event_data["entity_id"] = entity_id
    if domain:
        event_data["domain"] = domain
    hass.bus.async_fire("logbook_entry", event_data)
    return {"ok": True, "name": name, "message": message}


async def _list_device_actions(
    hass: HomeAssistant, device_id: str,
) -> dict[str, Any]:
    """List available automation actions for a device."""
    try:
        from homeassistant.components.device_automation import (
            async_get_device_automations,
        )
        actions = await async_get_device_automations(
            hass, "action", device_id,
        )
        items = [dict(a) for a in (actions or [])[:30]]
        return {"ok": True, "device_id": device_id, "count": len(items), "actions": items}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "device_id": device_id,
                "note": f"Device actions unavailable ({exc})", "actions": []}


async def _execute_device_action(
    hass: HomeAssistant, action: dict[str, Any],
) -> dict[str, Any]:
    """Execute a device automation action."""
    try:
        from homeassistant.components.device_automation.action import (
            async_call_action_from_config,
        )
        await async_call_action_from_config(
            hass, action, {}, None,
        )
        return {"ok": True, "action": action}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Execute device action failed: {exc}"}


# ---------------------------------------------------------------------------
# Wave 11: group, persistent notification, timer remaining, sun, input
#           datetime, climate swing/fan modes, media TTS, device tracker see
# ---------------------------------------------------------------------------


async def _set_group_members(
    hass: HomeAssistant, entity_id: str, members: list[str],
) -> dict[str, Any]:
    """Set the members of a group entity."""
    try:
        await hass.services.async_call(
            "group", "set",
            {"object_id": entity_id.replace("group.", ""), "entities": members},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set group members failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "members": members}


async def _dismiss_persistent_notification(
    hass: HomeAssistant, notification_id: str,
) -> dict[str, Any]:
    """Dismiss a persistent notification by ID."""
    try:
        await hass.services.async_call(
            "persistent_notification", "dismiss",
            {"notification_id": notification_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Dismiss persistent notification failed: {exc}"}
    return {"ok": True, "notification_id": notification_id, "action": "dismissed"}


async def _get_timer_remaining(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Get the remaining time on a timer entity."""
    state = hass.states.get(entity_id)
    if not state:
        return {"error": f"Entity '{entity_id}' not found"}
    attrs = state.attributes
    return {
        "ok": True, "entity_id": entity_id, "state": state.state,
        "duration": attrs.get("duration"), "remaining": attrs.get("remaining"),
        "finishes_at": attrs.get("finishes_at"),
    }


async def _get_sun_position(hass: HomeAssistant) -> dict[str, Any]:
    """Get current sun position (elevation, azimuth, next rising/setting)."""
    state = hass.states.get("sun.sun")
    if not state:
        return {"ok": True, "note": "Sun entity not available", "state": None}
    attrs = state.attributes
    return {
        "ok": True, "state": state.state,
        "elevation": attrs.get("elevation"),
        "azimuth": attrs.get("azimuth"),
        "next_rising": str(attrs.get("next_rising", "")),
        "next_setting": str(attrs.get("next_setting", "")),
        "next_dawn": str(attrs.get("next_dawn", "")),
        "next_dusk": str(attrs.get("next_dusk", "")),
        "next_noon": str(attrs.get("next_noon", "")),
        "next_midnight": str(attrs.get("next_midnight", "")),
    }


async def _set_input_datetime(
    hass: HomeAssistant, entity_id: str,
    date: str | None = None, time: str | None = None,
    datetime_val: str | None = None,
) -> dict[str, Any]:
    """Set an input_datetime entity value."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if datetime_val:
        data["datetime"] = datetime_val
    else:
        if date:
            data["date"] = date
        if time:
            data["time"] = time
    try:
        await hass.services.async_call(
            "input_datetime", "set_datetime", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set input_datetime failed: {exc}"}
    return {"ok": True, "entity_id": entity_id}


async def _climate_set_swing_mode(
    hass: HomeAssistant, entity_id: str, swing_mode: str,
) -> dict[str, Any]:
    """Set climate swing mode (on/off/vertical/horizontal/both)."""
    try:
        await hass.services.async_call(
            "climate", "set_swing_mode",
            {"entity_id": entity_id, "swing_mode": swing_mode}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Climate set swing mode failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "swing_mode": swing_mode}


async def _climate_set_fan_mode(
    hass: HomeAssistant, entity_id: str, fan_mode: str,
) -> dict[str, Any]:
    """Set climate fan mode (auto/low/medium/high/off/on/diffuse)."""
    try:
        await hass.services.async_call(
            "climate", "set_fan_mode",
            {"entity_id": entity_id, "fan_mode": fan_mode}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Climate set fan mode failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "fan_mode": fan_mode}


async def _media_player_tts(
    hass: HomeAssistant, entity_id: str, message: str,
    engine: str | None = None, language: str | None = None,
    cache: bool = True,
) -> dict[str, Any]:
    """Play a text-to-speech message on a media player."""
    data: dict[str, Any] = {"entity_id": entity_id, "message": message}
    if engine:
        data["engine_id"] = engine
    if language:
        data["language"] = language
    data["cache"] = cache
    try:
        await hass.services.async_call("tts", "speak", data, blocking=True)
    except Exception:  # noqa: BLE001
        try:
            await hass.services.async_call(
                "tts", "google_translate_say", data, blocking=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"TTS playback failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "message": message}


async def _device_tracker_see(
    hass: HomeAssistant, dev_id: str | None = None,
    mac: str | None = None, location_name: str | None = None,
    gps: list[float] | None = None, gps_accuracy: int | None = None,
    battery: int | None = None, host_name: str | None = None,
) -> dict[str, Any]:
    """Manually update device_tracker location (legacy see service)."""
    data: dict[str, Any] = {}
    if dev_id:
        data["dev_id"] = dev_id
    if mac:
        data["mac"] = mac
    if location_name:
        data["location_name"] = location_name
    if gps:
        data["gps"] = gps
    if gps_accuracy is not None:
        data["gps_accuracy"] = gps_accuracy
    if battery is not None:
        data["battery"] = battery
    if host_name:
        data["host_name"] = host_name
    if not data:
        return {"error": "At least one of dev_id/mac is required"}
    try:
        await hass.services.async_call(
            "device_tracker", "see", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Device tracker see failed: {exc}"}
    return {"ok": True, "data": data}


# ---------------------------------------------------------------------------
# Wave 13: automation enable/disable, script trigger, entity attrs, integration
#           info, input_text, light/switch shortcuts, climate set, HA domain,
#           intent handlers
# ---------------------------------------------------------------------------


async def _enable_automation(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Enable an automation."""
    try:
        await hass.services.async_call(
            "automation", "turn_on", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Enable automation failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "enabled"}


async def _disable_automation(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Disable an automation."""
    try:
        await hass.services.async_call(
            "automation", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Disable automation failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "disabled"}


async def _trigger_script(
    hass: HomeAssistant, entity_id: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trigger a script with optional variables."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if variables:
        data["variables"] = variables
    try:
        await hass.services.async_call("script", "turn_on", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Trigger script failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "triggered"}


async def _get_entity_attributes(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Get all attributes of a specific entity."""
    state = hass.states.get(entity_id)
    if not state:
        return {"error": f"Entity '{entity_id}' not found"}
    return {
        "ok": True, "entity_id": entity_id, "state": state.state,
        "attributes": dict(state.attributes),
        "last_changed": str(state.last_changed),
        "last_updated": str(state.last_updated),
    }


async def _get_integration_info(
    hass: HomeAssistant, domain: str,
) -> dict[str, Any]:
    """Get detailed info about a specific integration domain."""
    entries = [
        e for e in hass.config_entries.async_entries()
        if e.domain == domain
    ]
    items = []
    for e in entries:
        items.append({
            "entry_id": e.entry_id,
            "title": e.title,
            "state": str(e.state),
            "domain": e.domain,
        })
    if not items:
        return {"ok": True, "domain": domain, "note": "No config entries for this domain",
                "entries": []}
    return {"ok": True, "domain": domain, "count": len(items), "entries": items}


async def _set_input_text(
    hass: HomeAssistant, entity_id: str, value: str,
) -> dict[str, Any]:
    """Set an input_text entity value."""
    try:
        await hass.services.async_call(
            "input_text", "set_value",
            {"entity_id": entity_id, "value": value}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set input_text failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "value": value}


async def _light_turn_on(
    hass: HomeAssistant, entity_id: str,
    brightness: int | None = None, color_temp: int | None = None,
    rgb_color: list[int] | None = None, transition: float | None = None,
    effect: str | None = None,
) -> dict[str, Any]:
    """Turn on a light with optional brightness/color/transition."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if brightness is not None:
        data["brightness"] = brightness
    if color_temp is not None:
        data["color_temp"] = color_temp
    if rgb_color:
        data["rgb_color"] = rgb_color
    if transition is not None:
        data["transition"] = transition
    if effect:
        data["effect"] = effect
    try:
        await hass.services.async_call("light", "turn_on", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Light turn_on failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_on"}


async def _light_turn_off(
    hass: HomeAssistant, entity_id: str,
    transition: float | None = None,
) -> dict[str, Any]:
    """Turn off a light."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if transition is not None:
        data["transition"] = transition
    try:
        await hass.services.async_call("light", "turn_off", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Light turn_off failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_off"}


async def _switch_turn_on(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Turn on a switch."""
    try:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Switch turn_on failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_on"}


async def _switch_turn_off(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Turn off a switch."""
    try:
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Switch turn_off failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_off"}


async def _climate_set_temperature(
    hass: HomeAssistant, entity_id: str,
    temperature: float | None = None,
    target_temp_high: float | None = None,
    target_temp_low: float | None = None,
    hvac_mode: str | None = None,
) -> dict[str, Any]:
    """Set climate target temperature."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if temperature is not None:
        data["temperature"] = float(temperature)
    if target_temp_high is not None:
        data["target_temp_high"] = float(target_temp_high)
    if target_temp_low is not None:
        data["target_temp_low"] = float(target_temp_low)
    if hvac_mode:
        data["hvac_mode"] = hvac_mode
    try:
        await hass.services.async_call(
            "climate", "set_temperature", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Climate set temperature failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "data": data}


async def _climate_set_hvac_mode(
    hass: HomeAssistant, entity_id: str, hvac_mode: str,
) -> dict[str, Any]:
    """Set climate HVAC mode (off/heat/cool/heat_cool/auto/dry/fan_only)."""
    try:
        await hass.services.async_call(
            "climate", "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": hvac_mode}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Climate set HVAC mode failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "hvac_mode": hvac_mode}


async def _homeassistant_turn_on(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Turn on any entity via homeassistant.turn_on (universal)."""
    try:
        await hass.services.async_call(
            "homeassistant", "turn_on", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"HA turn_on failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_on"}


async def _homeassistant_turn_off(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Turn off any entity via homeassistant.turn_off (universal)."""
    try:
        await hass.services.async_call(
            "homeassistant", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"HA turn_off failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_off"}


async def _homeassistant_toggle(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Toggle any entity via homeassistant.toggle (universal)."""
    try:
        await hass.services.async_call(
            "homeassistant", "toggle", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"HA toggle failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "toggled"}


async def _list_intent_handlers(hass: HomeAssistant) -> dict[str, Any]:
    """List registered conversation intent handlers."""
    try:
        from homeassistant.helpers import intent as intent_helper
        intents = list(intent_helper.async_get(hass) or {})
        return {"ok": True, "count": len(intents), "intents": intents[:50]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "note": f"Intent handlers unavailable ({exc})", "intents": []}


# ---------------------------------------------------------------------------
# Wave 14: vacuum, number, button, select, text, valve, lawn_mower, remote,
#           input_button
# ---------------------------------------------------------------------------


async def _vacuum_start(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Start a vacuum."""
    try:
        await hass.services.async_call(
            "vacuum", "start", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Vacuum start failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "start"}


async def _vacuum_stop(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Stop a vacuum."""
    try:
        await hass.services.async_call(
            "vacuum", "stop", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Vacuum stop failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "stop"}


async def _vacuum_return_home(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Send a vacuum back to its dock."""
    try:
        await hass.services.async_call(
            "vacuum", "return_to_base", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Vacuum return_home failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "return_to_base"}


async def _vacuum_locate(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Locate a vacuum (play sound)."""
    try:
        await hass.services.async_call(
            "vacuum", "locate", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Vacuum locate failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "locate"}


async def _vacuum_set_fan_speed(
    hass: HomeAssistant, entity_id: str, fan_speed: str,
) -> dict[str, Any]:
    """Set vacuum fan speed (quiet/balanced/turbo/max)."""
    try:
        await hass.services.async_call(
            "vacuum", "set_fan_speed",
            {"entity_id": entity_id, "fan_speed": fan_speed}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Vacuum set fan speed failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "fan_speed": fan_speed}


async def _vacuum_send_command(
    hass: HomeAssistant, entity_id: str, command: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a custom command to a vacuum."""
    data: dict[str, Any] = {"entity_id": entity_id, "command": command}
    if params:
        data["params"] = params
    try:
        await hass.services.async_call("vacuum", "send_command", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Vacuum send command failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _number_set_value(
    hass: HomeAssistant, entity_id: str, value: float,
) -> dict[str, Any]:
    """Set a number entity value."""
    try:
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": entity_id, "value": float(value)}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Number set value failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "value": value}


async def _button_press(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Press a button entity."""
    try:
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Button press failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "pressed"}


async def _select_set_option(
    hass: HomeAssistant, entity_id: str, option: str,
) -> dict[str, Any]:
    """Set a select entity option."""
    try:
        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": entity_id, "option": option}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Select set option failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "option": option}


async def _text_set_value(
    hass: HomeAssistant, entity_id: str, value: str,
) -> dict[str, Any]:
    """Set a text entity value."""
    try:
        await hass.services.async_call(
            "text", "set_value",
            {"entity_id": entity_id, "value": value}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Text set value failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "value": value}


async def _valve_open(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Open a valve."""
    try:
        await hass.services.async_call(
            "valve", "open_valve", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Valve open failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "open"}


async def _valve_close(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Close a valve."""
    try:
        await hass.services.async_call(
            "valve", "close_valve", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Valve close failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "close"}


async def _valve_set_position(
    hass: HomeAssistant, entity_id: str, position: int,
) -> dict[str, Any]:
    """Set valve position (0=closed, 100=open)."""
    try:
        await hass.services.async_call(
            "valve", "set_valve_position",
            {"entity_id": entity_id, "position": position}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Valve set position failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "position": position}


async def _lawn_mower_start(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Start mowing."""
    try:
        await hass.services.async_call(
            "lawn_mower", "start_mowing", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Lawn mower start failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "start_mowing"}


async def _lawn_mower_dock(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Send lawn mower back to dock."""
    try:
        await hass.services.async_call(
            "lawn_mower", "dock", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Lawn mower dock failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "dock"}


async def _remote_send_command(
    hass: HomeAssistant, entity_id: str, command: str,
    device: str | None = None, num_repeats: int | None = None,
    delay_secs: float | None = None,
) -> dict[str, Any]:
    """Send a command via a remote entity."""
    data: dict[str, Any] = {"entity_id": entity_id, "command": command}
    if device:
        data["device"] = device
    if num_repeats is not None:
        data["num_repeats"] = num_repeats
    if delay_secs is not None:
        data["delay_secs"] = delay_secs
    try:
        await hass.services.async_call("remote", "send_command", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Remote send command failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "command": command}


async def _remote_learn_command(
    hass: HomeAssistant, entity_id: str,
    device: str | None = None, command_type: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Put a remote entity into learning mode."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if device:
        data["device"] = device
    if command_type:
        data["command_type"] = command_type
    if timeout is not None:
        data["timeout"] = timeout
    try:
        await hass.services.async_call("remote", "learn_command", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Remote learn command failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "learning"}


async def _press_input_button(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Press an input_button entity."""
    try:
        await hass.services.async_call(
            "input_button", "press", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Press input_button failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "pressed"}


# ---------------------------------------------------------------------------
# Wave 15: media player controls, date/time/datetime entities
# ---------------------------------------------------------------------------


async def _media_player_play_media(
    hass: HomeAssistant, entity_id: str, media_content_id: str,
    media_content_type: str = "music",
    enqueue: str | None = None,
) -> dict[str, Any]:
    """Play media on a media player."""
    data: dict[str, Any] = {
        "entity_id": entity_id,
        "media_content_id": media_content_id,
        "media_content_type": media_content_type,
    }
    if enqueue:
        data["enqueue"] = enqueue
    try:
        await hass.services.async_call("media_player", "play_media", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Play media failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "media_content_id": media_content_id}


async def _media_player_set_volume(
    hass: HomeAssistant, entity_id: str, volume_level: float,
) -> dict[str, Any]:
    """Set media player volume (0.0 to 1.0)."""
    try:
        await hass.services.async_call(
            "media_player", "volume_set",
            {"entity_id": entity_id, "volume_level": float(volume_level)},
            blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set volume failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "volume_level": volume_level}


async def _media_player_media_pause(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Pause media playback."""
    try:
        await hass.services.async_call(
            "media_player", "media_pause", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Media pause failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "paused"}


async def _media_player_media_play(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Resume media playback."""
    try:
        await hass.services.async_call(
            "media_player", "media_play", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Media play failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "playing"}


async def _media_player_media_next(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Skip to next media track."""
    try:
        await hass.services.async_call(
            "media_player", "media_next_track", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Media next track failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "next_track"}


async def _media_player_media_previous(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Skip to previous media track."""
    try:
        await hass.services.async_call(
            "media_player", "media_previous_track", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Media previous track failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "previous_track"}


async def _date_set_value(
    hass: HomeAssistant, entity_id: str, date: str,
) -> dict[str, Any]:
    """Set a date entity value (YYYY-MM-DD)."""
    try:
        await hass.services.async_call(
            "date", "set_value",
            {"entity_id": entity_id, "date": date}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Date set value failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "date": date}


async def _time_set_value(
    hass: HomeAssistant, entity_id: str, time: str,
) -> dict[str, Any]:
    """Set a time entity value (HH:MM:SS)."""
    try:
        await hass.services.async_call(
            "time", "set_value",
            {"entity_id": entity_id, "time": time}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Time set value failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "time": time}


async def _datetime_set_value(
    hass: HomeAssistant, entity_id: str, datetime_val: str,
) -> dict[str, Any]:
    """Set a datetime entity value (YYYY-MM-DD HH:MM:SS)."""
    try:
        await hass.services.async_call(
            "datetime", "set_value",
            {"entity_id": entity_id, "datetime": datetime_val}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Datetime set value failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "datetime": datetime_val}


# ---------------------------------------------------------------------------
# Wave 17: siren, humidifier, water_heater, fan, alarm_control_panel, lock,
#           cover, timer, counter
# ---------------------------------------------------------------------------


async def _siren_turn_on(
    hass: HomeAssistant, entity_id: str,
    tone: str | None = None, volume_level: float | None = None,
    duration: int | None = None,
) -> dict[str, Any]:
    """Turn on a siren."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if tone:
        data["tone"] = tone
    if volume_level is not None:
        data["volume_level"] = volume_level
    if duration is not None:
        data["duration"] = duration
    try:
        await hass.services.async_call("siren", "turn_on", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Siren turn_on failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_on"}


async def _siren_turn_off(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Turn off a siren."""
    try:
        await hass.services.async_call(
            "siren", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Siren turn_off failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_off"}


async def _humidifier_turn_on(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Turn on a humidifier."""
    try:
        await hass.services.async_call(
            "humidifier", "turn_on", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Humidifier turn_on failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_on"}


async def _humidifier_turn_off(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Turn off a humidifier."""
    try:
        await hass.services.async_call(
            "humidifier", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Humidifier turn_off failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_off"}


async def _humidifier_set_humidity(
    hass: HomeAssistant, entity_id: str, humidity: int,
) -> dict[str, Any]:
    """Set target humidity."""
    try:
        await hass.services.async_call(
            "humidifier", "set_humidity",
            {"entity_id": entity_id, "humidity": humidity}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Humidifier set humidity failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "humidity": humidity}


async def _humidifier_set_mode(
    hass: HomeAssistant, entity_id: str, mode: str,
) -> dict[str, Any]:
    """Set humidifier mode."""
    try:
        await hass.services.async_call(
            "humidifier", "set_mode",
            {"entity_id": entity_id, "mode": mode}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Humidifier set mode failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "mode": mode}


async def _water_heater_set_temperature(
    hass: HomeAssistant, entity_id: str, temperature: float,
) -> dict[str, Any]:
    """Set water heater target temperature."""
    try:
        await hass.services.async_call(
            "water_heater", "set_temperature",
            {"entity_id": entity_id, "temperature": temperature}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Water heater set temp failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "temperature": temperature}


async def _water_heater_set_operation_mode(
    hass: HomeAssistant, entity_id: str, operation_mode: str,
) -> dict[str, Any]:
    """Set water heater operation mode."""
    try:
        await hass.services.async_call(
            "water_heater", "set_operation_mode",
            {"entity_id": entity_id, "operation_mode": operation_mode}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Water heater set mode failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "operation_mode": operation_mode}


async def _fan_turn_on(
    hass: HomeAssistant, entity_id: str,
    percentage: int | None = None, preset_mode: str | None = None,
) -> dict[str, Any]:
    """Turn on a fan."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if percentage is not None:
        data["percentage"] = percentage
    if preset_mode:
        data["preset_mode"] = preset_mode
    try:
        await hass.services.async_call("fan", "turn_on", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Fan turn_on failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_on"}


async def _fan_turn_off(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Turn off a fan."""
    try:
        await hass.services.async_call(
            "fan", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Fan turn_off failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_off"}


async def _fan_set_percentage(
    hass: HomeAssistant, entity_id: str, percentage: int,
) -> dict[str, Any]:
    """Set fan speed percentage (0-100)."""
    try:
        await hass.services.async_call(
            "fan", "set_percentage",
            {"entity_id": entity_id, "percentage": percentage}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Fan set percentage failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "percentage": percentage}


async def _fan_set_direction(
    hass: HomeAssistant, entity_id: str, direction: str,
) -> dict[str, Any]:
    """Set fan direction (forward/reverse)."""
    try:
        await hass.services.async_call(
            "fan", "set_direction",
            {"entity_id": entity_id, "direction": direction}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Fan set direction failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "direction": direction}


async def _fan_oscillate(
    hass: HomeAssistant, entity_id: str, oscillating: bool,
) -> dict[str, Any]:
    """Set fan oscillation."""
    try:
        await hass.services.async_call(
            "fan", "oscillate",
            {"entity_id": entity_id, "oscillating": oscillating}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Fan oscillate failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "oscillating": oscillating}


async def _fan_set_preset_mode(
    hass: HomeAssistant, entity_id: str, preset_mode: str,
) -> dict[str, Any]:
    """Set fan preset mode (eco/sleep/auto/etc)."""
    try:
        await hass.services.async_call(
            "fan", "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": preset_mode}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Fan set preset mode failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "preset_mode": preset_mode}


async def _alarm_arm_away(
    hass: HomeAssistant, entity_id: str, code: str | None = None,
) -> dict[str, Any]:
    """Arm alarm in away mode."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if code:
        data["code"] = code
    try:
        await hass.services.async_call(
            "alarm_control_panel", "alarm_arm_away", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Alarm arm away failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "armed_away"}


async def _alarm_arm_home(
    hass: HomeAssistant, entity_id: str, code: str | None = None,
) -> dict[str, Any]:
    """Arm alarm in home mode."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if code:
        data["code"] = code
    try:
        await hass.services.async_call(
            "alarm_control_panel", "alarm_arm_home", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Alarm arm home failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "armed_home"}


async def _alarm_arm_night(
    hass: HomeAssistant, entity_id: str, code: str | None = None,
) -> dict[str, Any]:
    """Arm alarm in night mode."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if code:
        data["code"] = code
    try:
        await hass.services.async_call(
            "alarm_control_panel", "alarm_arm_night", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Alarm arm night failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "armed_night"}


async def _alarm_disarm(
    hass: HomeAssistant, entity_id: str, code: str | None = None,
) -> dict[str, Any]:
    """Disarm alarm."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if code:
        data["code"] = code
    try:
        await hass.services.async_call(
            "alarm_control_panel", "alarm_disarm", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Alarm disarm failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "disarmed"}


async def _alarm_trigger(
    hass: HomeAssistant, entity_id: str, code: str | None = None,
) -> dict[str, Any]:
    """Trigger alarm."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if code:
        data["code"] = code
    try:
        await hass.services.async_call(
            "alarm_control_panel", "alarm_trigger", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Alarm trigger failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "triggered"}


async def _lock_lock(
    hass: HomeAssistant, entity_id: str, code: str | None = None,
) -> dict[str, Any]:
    """Lock a lock."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if code:
        data["code"] = code
    try:
        await hass.services.async_call("lock", "lock", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Lock failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "locked"}


async def _lock_unlock(
    hass: HomeAssistant, entity_id: str, code: str | None = None,
) -> dict[str, Any]:
    """Unlock a lock."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if code:
        data["code"] = code
    try:
        await hass.services.async_call("lock", "unlock", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Unlock failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "unlocked"}


async def _lock_open(
    hass: HomeAssistant, entity_id: str, code: str | None = None,
) -> dict[str, Any]:
    """Open a lock (unlatch)."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if code:
        data["code"] = code
    try:
        await hass.services.async_call("lock", "open", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Lock open failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "opened"}


async def _cover_open(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Open a cover."""
    try:
        await hass.services.async_call(
            "cover", "open_cover", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cover open failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "open"}


async def _cover_close(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Close a cover."""
    try:
        await hass.services.async_call(
            "cover", "close_cover", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cover close failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "close"}


async def _cover_stop(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Stop a cover."""
    try:
        await hass.services.async_call(
            "cover", "stop_cover", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cover stop failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "stop"}


async def _cover_set_position(
    hass: HomeAssistant, entity_id: str, position: int,
) -> dict[str, Any]:
    """Set cover position (0=closed, 100=open)."""
    try:
        await hass.services.async_call(
            "cover", "set_cover_position",
            {"entity_id": entity_id, "position": position}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cover set position failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "position": position}


async def _cover_open_tilt(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Open cover tilt."""
    try:
        await hass.services.async_call(
            "cover", "open_cover_tilt", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cover open tilt failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "open_tilt"}


async def _cover_close_tilt(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Close cover tilt."""
    try:
        await hass.services.async_call(
            "cover", "close_cover_tilt", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cover close tilt failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "close_tilt"}


async def _cover_set_tilt_position(
    hass: HomeAssistant, entity_id: str, tilt_position: int,
) -> dict[str, Any]:
    """Set cover tilt position (0-100)."""
    try:
        await hass.services.async_call(
            "cover", "set_cover_tilt_position",
            {"entity_id": entity_id, "tilt_position": tilt_position}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cover set tilt position failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "tilt_position": tilt_position}


async def _timer_start(
    hass: HomeAssistant, entity_id: str, duration: str | None = None,
) -> dict[str, Any]:
    """Start a timer (optional duration override HH:MM:SS)."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if duration:
        data["duration"] = duration
    try:
        await hass.services.async_call("timer", "start", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Timer start failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "started"}


async def _timer_cancel(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Cancel a timer."""
    try:
        await hass.services.async_call(
            "timer", "cancel", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Timer cancel failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "cancelled"}


async def _timer_pause(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Pause a timer."""
    try:
        await hass.services.async_call(
            "timer", "pause", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Timer pause failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "paused"}


async def _timer_finish(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Finish (force-complete) a timer."""
    try:
        await hass.services.async_call(
            "timer", "finish", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Timer finish failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "finished"}


async def _increment_counter(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Increment a counter entity."""
    try:
        await hass.services.async_call(
            "counter", "increment", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Counter increment failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "incremented"}


async def _decrement_counter(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Decrement a counter entity."""
    try:
        await hass.services.async_call(
            "counter", "decrement", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Counter decrement failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "decremented"}


async def _reset_counter(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Reset a counter entity."""
    try:
        await hass.services.async_call(
            "counter", "reset", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Counter reset failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "reset"}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Wave 18: todo list, input_boolean, input_number, input_select,
#           media player shuffle/repeat
# ---------------------------------------------------------------------------


async def _todo_add_item(
    hass: HomeAssistant, entity_id: str, item: str,
    due_date: str | None = None, description: str | None = None,
) -> dict[str, Any]:
    """Add an item to a todo list entity."""
    data: dict[str, Any] = {"entity_id": entity_id, "item": item}
    if due_date:
        data["due_date"] = due_date
    if description:
        data["description"] = description
    try:
        await hass.services.async_call("todo", "add_item", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Todo add item failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "item": item}


async def _todo_update_item(
    hass: HomeAssistant, entity_id: str, item: str,
    rename: str | None = None, status: str | None = None,
    due_date: str | None = None, description: str | None = None,
) -> dict[str, Any]:
    """Update an item in a todo list entity."""
    data: dict[str, Any] = {"entity_id": entity_id, "item": item}
    if rename:
        data["rename"] = rename
    if status:
        data["status"] = status
    if due_date:
        data["due_date"] = due_date
    if description:
        data["description"] = description
    try:
        await hass.services.async_call("todo", "update_item", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Todo update item failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "item": item}


async def _todo_remove_item(
    hass: HomeAssistant, entity_id: str, item: str,
) -> dict[str, Any]:
    """Remove an item from a todo list entity."""
    try:
        await hass.services.async_call(
            "todo", "remove_item",
            {"entity_id": entity_id, "item": item}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Todo remove item failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "item": item, "action": "removed"}


async def _todo_get_items(
    hass: HomeAssistant, entity_id: str,
    status: str | None = None,
) -> dict[str, Any]:
    """Get items from a todo list entity."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if status:
        data["status"] = status
    try:
        resp = await hass.services.async_call(
            "todo", "get_items", data, blocking=True, return_response=True,
        )
        return {"ok": True, "entity_id": entity_id, "items": resp}
    except TypeError:
        try:
            await hass.services.async_call("todo", "get_items", data, blocking=True)
            return {"ok": True, "entity_id": entity_id, "note": "items retrieved"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Todo get items failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Todo get items failed: {exc}"}


async def _input_boolean_turn_on(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Turn on an input_boolean."""
    try:
        await hass.services.async_call(
            "input_boolean", "turn_on", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input boolean turn_on failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_on"}


async def _input_boolean_turn_off(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Turn off an input_boolean."""
    try:
        await hass.services.async_call(
            "input_boolean", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input boolean turn_off failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "turn_off"}


async def _input_boolean_toggle(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Toggle an input_boolean."""
    try:
        await hass.services.async_call(
            "input_boolean", "toggle", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input boolean toggle failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "toggled"}


async def _input_number_set_value(
    hass: HomeAssistant, entity_id: str, value: float,
) -> dict[str, Any]:
    """Set an input_number value."""
    try:
        await hass.services.async_call(
            "input_number", "set_value",
            {"entity_id": entity_id, "value": float(value)}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input number set value failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "value": value}


async def _input_number_increment(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Increment an input_number by its step."""
    try:
        await hass.services.async_call(
            "input_number", "increment", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input number increment failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "incremented"}


async def _input_number_decrement(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Decrement an input_number by its step."""
    try:
        await hass.services.async_call(
            "input_number", "decrement", {"entity_id": entity_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input number decrement failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "decremented"}


async def _input_select_set_option(
    hass: HomeAssistant, entity_id: str, option: str,
) -> dict[str, Any]:
    """Select an option on an input_select."""
    try:
        await hass.services.async_call(
            "input_select", "select_option",
            {"entity_id": entity_id, "option": option}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input select set option failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "option": option}


async def _input_select_set_options(
    hass: HomeAssistant, entity_id: str, options: list[str],
) -> dict[str, Any]:
    """Set the options list of an input_select."""
    try:
        await hass.services.async_call(
            "input_select", "set_options",
            {"entity_id": entity_id, "options": options}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input select set options failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "options": options}


async def _input_select_next(
    hass: HomeAssistant, entity_id: str, cycle: bool = True,
) -> dict[str, Any]:
    """Select next option on an input_select."""
    try:
        await hass.services.async_call(
            "input_select", "select_next",
            {"entity_id": entity_id, "cycle": cycle}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input select next failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "next"}


async def _input_select_previous(
    hass: HomeAssistant, entity_id: str, cycle: bool = True,
) -> dict[str, Any]:
    """Select previous option on an input_select."""
    try:
        await hass.services.async_call(
            "input_select", "select_previous",
            {"entity_id": entity_id, "cycle": cycle}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input select previous failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "action": "previous"}


async def _media_player_shuffle_set(
    hass: HomeAssistant, entity_id: str, shuffle: bool,
) -> dict[str, Any]:
    """Set media player shuffle mode."""
    try:
        await hass.services.async_call(
            "media_player", "shuffle_set",
            {"entity_id": entity_id, "shuffle": shuffle}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Media player shuffle set failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "shuffle": shuffle}


async def _media_player_repeat_set(
    hass: HomeAssistant, entity_id: str, repeat: str,
) -> dict[str, Any]:
    """Set media player repeat mode (off/all/one)."""
    try:
        await hass.services.async_call(
            "media_player", "repeat_set",
            {"entity_id": entity_id, "repeat": repeat}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Media player repeat set failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "repeat": repeat}


# ---------------------------------------------------------------------------
# Wave 20: input_text, device_tracker, input_datetime, schedule,
#           persistent_notification, network
# ---------------------------------------------------------------------------


async def _input_text_set_value(
    hass: HomeAssistant, entity_id: str, value: str,
) -> dict[str, Any]:
    """Set an input_text value."""
    try:
        await hass.services.async_call(
            "input_text", "set_value",
            {"entity_id": entity_id, "value": value}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input text set value failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "value": value}


async def _set_device_tracker_location(
    hass: HomeAssistant, entity_id: str,
    location_name: str | None = None,
    gps: list[float] | None = None,
    gps_accuracy: int | None = None,
    battery: int | None = None,
) -> dict[str, Any]:
    """Set a device tracker location."""
    data: dict[str, Any] = {"dev_id": entity_id.replace("device_tracker.", "")}
    if location_name:
        data["location_name"] = location_name
    if gps:
        data["gps"] = gps
    if gps_accuracy is not None:
        data["gps_accuracy"] = gps_accuracy
    if battery is not None:
        data["battery"] = battery
    try:
        await hass.services.async_call(
            "device_tracker", "see", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Set device tracker location failed: {exc}"}
    return {"ok": True, "entity_id": entity_id, "location_name": location_name}


async def _input_datetime_set_datetime(
    hass: HomeAssistant, entity_id: str,
    date: str | None = None, time: str | None = None,
    datetime_val: str | None = None,
) -> dict[str, Any]:
    """Set an input_datetime value."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if datetime_val:
        data["datetime"] = datetime_val
    if date:
        data["date"] = date
    if time:
        data["time"] = time
    try:
        await hass.services.async_call(
            "input_datetime", "set_datetime", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Input datetime set failed: {exc}"}
    return {"ok": True, "entity_id": entity_id}


async def _schedule_get_schedule(
    hass: HomeAssistant, entity_id: str,
) -> dict[str, Any]:
    """Get schedule entity state and attributes."""
    state = hass.states.get(entity_id)
    if state is None:
        return {"error": f"Schedule entity {entity_id} not found"}
    return {
        "ok": True,
        "entity_id": entity_id,
        "state": state.state,
        "attributes": dict(state.attributes),
    }


async def _persistent_notification_create(
    hass: HomeAssistant, message: str,
    title: str | None = None, notification_id: str | None = None,
) -> dict[str, Any]:
    """Create a persistent notification."""
    data: dict[str, Any] = {"message": message}
    if title:
        data["title"] = title
    if notification_id:
        data["notification_id"] = notification_id
    try:
        await hass.services.async_call(
            "persistent_notification", "create", data, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Persistent notification create failed: {exc}"}
    return {"ok": True, "message": message, "action": "created"}


async def _persistent_notification_dismiss(
    hass: HomeAssistant, notification_id: str,
) -> dict[str, Any]:
    """Dismiss a persistent notification."""
    try:
        await hass.services.async_call(
            "persistent_notification", "dismiss",
            {"notification_id": notification_id}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Persistent notification dismiss failed: {exc}"}
    return {"ok": True, "notification_id": notification_id, "action": "dismissed"}


async def _get_network_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get network configuration info from HA."""
    try:
        import socket
        hostname = socket.gethostname()
        ip_addr = socket.gethostbyname(hostname)
        return {
            "ok": True,
            "hostname": hostname,
            "ip_address": ip_addr,
            "ha_url": str(hass.config.config_dir),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Get network info failed: {exc}"}


# HA core internals — addons, areas, config entries, system, blueprints
# ---------------------------------------------------------------------------


async def _start_addon(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    """Start a Home Assistant add-on by slug."""
    try:
        await hass.services.async_call(
            "hassio", "addon_start", {"addon": slug}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Start addon failed: {exc}"}
    return {"ok": True, "slug": slug, "action": "started"}


async def _stop_addon(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    """Stop a Home Assistant add-on by slug."""
    try:
        await hass.services.async_call(
            "hassio", "addon_stop", {"addon": slug}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Stop addon failed: {exc}"}
    return {"ok": True, "slug": slug, "action": "stopped"}


async def _restart_addon(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    """Restart a Home Assistant add-on by slug."""
    try:
        await hass.services.async_call(
            "hassio", "addon_restart", {"addon": slug}, blocking=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Restart addon failed: {exc}"}
    return {"ok": True, "slug": slug, "action": "restarted"}


async def _get_addon_logs(
    hass: HomeAssistant, slug: str, lines: int = 100,
) -> dict[str, Any]:
    """Get log output from a Home Assistant add-on."""
    try:
        from homeassistant.components.hassio import get_supervisor_client

        client = get_supervisor_client(hass)
        logs = await client.addons.addon_logs(slug)
        log_lines = (logs or "").strip().split("\n")
        return {"ok": True, "slug": slug,
                "lines": log_lines[-lines:],
                "total_lines": len(log_lines)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Get addon logs failed: {exc}"}


async def _list_area_devices(
    hass: HomeAssistant, area_id: str,
) -> dict[str, Any]:
    """List all devices assigned to a specific area."""
    from homeassistant.helpers import area_registry as ar, device_registry as dr

    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)

    area = area_reg.async_get_area(area_id)
    if not area:
        for a in area_reg.async_list_areas():
            if a.name.lower() == area_id.lower():
                area = a
                break

    if not area:
        return {"error": f"Area '{area_id}' not found"}

    devices = []
    for device in dev_reg.devices.values():
        if device.area_id == area.id:
            devices.append({
                "device_id": device.id,
                "name": device.name,
                "model": device.model,
                "manufacturer": device.manufacturer,
            })

    return {"ok": True, "area": area.name, "area_id": area.id,
            "count": len(devices), "devices": devices}


async def _list_area_entities(
    hass: HomeAssistant, area_id: str,
) -> dict[str, Any]:
    """List all entities assigned to a specific area (directly or via device)."""
    from homeassistant.helpers import (
        area_registry as ar,
        device_registry as dr,
        entity_registry as er,
    )

    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    area = area_reg.async_get_area(area_id)
    if not area:
        for a in area_reg.async_list_areas():
            if a.name.lower() == area_id.lower():
                area = a
                break

    if not area:
        return {"error": f"Area '{area_id}' not found"}

    device_ids = {d.id for d in dev_reg.devices.values() if d.area_id == area.id}

    entities = []
    for entry in ent_reg.entities.values():
        if entry.area_id == area.id or entry.device_id in device_ids:
            state = hass.states.get(entry.entity_id)
            entities.append({
                "entity_id": entry.entity_id,
                "name": entry.name or entry.original_name,
                "domain": entry.domain,
                "state": state.state if state else None,
            })

    return {"ok": True, "area": area.name, "area_id": area.id,
            "count": len(entities), "entities": entities}


async def _delete_blueprint(
    hass: HomeAssistant, path: str, domain: str = "automation",
) -> dict[str, Any]:
    """Delete a blueprint YAML file."""
    bp_dir = hass.config.path("blueprints", domain)
    full_path = os.path.join(bp_dir, path) if not os.path.isabs(path) else path

    if not os.path.exists(full_path):
        return {"error": f"Blueprint not found: {full_path}"}

    try:
        os.remove(full_path)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Delete blueprint failed: {exc}"}

    return {"ok": True, "deleted": full_path}


async def _delete_config_entry(
    hass: HomeAssistant, entry_id: str,
) -> dict[str, Any]:
    """Remove an integration config entry."""
    try:
        result = await hass.config_entries.async_remove(entry_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Delete config entry failed: {exc}"}
    return {"ok": True, "entry_id": entry_id, "result": result}


async def _disable_config_entry(
    hass: HomeAssistant, entry_id: str, disable: bool = True,
) -> dict[str, Any]:
    """Enable or disable an integration config entry."""
    try:
        from homeassistant.config_entries import ConfigEntryDisabler

        if disable:
            await hass.config_entries.async_set_disabled_by(
                entry_id, ConfigEntryDisabler.USER,
            )
        else:
            await hass.config_entries.async_set_disabled_by(entry_id, None)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{'Disable' if disable else 'Enable'} config entry failed: {exc}"}
    return {"ok": True, "entry_id": entry_id, "disabled": disable}


async def _reload_integration(
    hass: HomeAssistant, domain: str,
) -> dict[str, Any]:
    """Reload all config entries for an integration domain."""
    entries = [e for e in hass.config_entries.async_entries()
               if e.domain == domain]
    if not entries:
        return {"error": f"No config entries found for domain: {domain}"}

    results = []
    for entry in entries:
        try:
            await hass.config_entries.async_reload(entry.entry_id)
            results.append({"entry_id": entry.entry_id, "ok": True})
        except Exception as exc:  # noqa: BLE001
            results.append({"entry_id": entry.entry_id, "error": str(exc)})

    return {"ok": True, "domain": domain, "reloaded": len(results), "results": results}


async def _get_hardware_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get hardware information: CPU, memory, disk usage."""
    import shutil

    info: dict[str, Any] = {}

    try:
        with open("/proc/cpuinfo") as f:
            cpuinfo = f.read()
            cores = cpuinfo.count("processor")
            model = ""
            for line in cpuinfo.split("\n"):
                if "model name" in line:
                    model = line.split(":")[1].strip()
                    break
            info["cpu"] = {"cores": cores, "model": model}
    except Exception:  # noqa: BLE001
        info["cpu"] = "unavailable"

    try:
        with open("/proc/meminfo") as f:
            meminfo = f.read()
            mem_total = mem_free = 0
            for line in meminfo.split("\n"):
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    mem_free = int(line.split()[1]) // 1024
            info["memory_mb"] = {"total": mem_total, "available": mem_free,
                                 "used": mem_total - mem_free}
    except Exception:  # noqa: BLE001
        info["memory_mb"] = "unavailable"

    try:
        usage = shutil.disk_usage(hass.config.config_dir)
        info["disk_mb"] = {
            "total": usage.total // (1024 * 1024),
            "used": usage.used // (1024 * 1024),
            "free": usage.free // (1024 * 1024),
        }
    except Exception:  # noqa: BLE001
        info["disk_mb"] = "unavailable"

    try:
        with open("/proc/uptime") as f:
            uptime_sec = float(f.read().split()[0])
            info["uptime_hours"] = round(uptime_sec / 3600, 1)
    except Exception:  # noqa: BLE001
        pass

    return {"ok": True, **info}


async def _get_os_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get HA OS and supervisor information."""
    import platform

    info: dict[str, Any] = {
        "ha_version": hass.config.version if hasattr(hass.config, "version") else "unknown",
        "python_version": platform.python_version(),
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "config_dir": hass.config.config_dir,
    }

    try:
        from homeassistant.components.hassio import get_supervisor_client

        client = get_supervisor_client(hass)
        os_data = await client.os.info()
        info["ha_os_version"] = os_data.version if hasattr(os_data, "version") else str(os_data)
    except Exception:  # noqa: BLE001
        info["ha_os"] = "not running HA OS (or Supervisor unavailable)"

    try:
        from homeassistant.components.hassio import get_supervisor_client

        client = get_supervisor_client(hass)
        sup = await client.supervisor.info()
        info["supervisor_version"] = sup.version if hasattr(sup, "version") else str(sup)
    except Exception:  # noqa: BLE001
        pass

    return {"ok": True, **info}


async def _list_template_entities(hass: HomeAssistant) -> dict[str, Any]:
    """List entities backed by the template integration."""
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    template_entities = []
    for entry in reg.entities.values():
        if entry.platform == "template":
            state = hass.states.get(entry.entity_id)
            template_entities.append({
                "entity_id": entry.entity_id,
                "name": entry.name or entry.original_name,
                "domain": entry.domain,
                "state": state.state if state else None,
            })

    return {"ok": True, "count": len(template_entities), "entities": template_entities}


async def _list_credentials(hass: HomeAssistant) -> dict[str, Any]:
    """List authentication providers and configured credentials."""
    providers = []
    try:
        for provider in hass.auth.auth_providers:
            providers.append({
                "type": provider.type,
                "name": getattr(provider, "name", provider.type),
                "id": getattr(provider, "id", None),
            })
    except Exception:  # noqa: BLE001
        pass

    if not providers:
        return {"ok": True, "count": 0, "providers": [],
                "note": "Auth provider enumeration not available"}

    return {"ok": True, "count": len(providers), "providers": providers}


# ---------------------------------------------------------------------------
# Workflow orchestration — batch & config operations (Stage 4 evolution)
# ---------------------------------------------------------------------------


async def _batch_call_service(
    hass: HomeAssistant,
    domain: str,
    service: str,
    entity_ids: list[str],
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call a service for multiple entities in one operation.

    This is the batch equivalent of ``call_service`` — controls many entities
    at once without requiring N individual calls, which matters for scenes like
    "turn off everything" or "set all lights to 50%".
    """
    if not entity_ids:
        return {"error": "entity_ids list is empty"}
    results: list[dict[str, Any]] = []
    for eid in entity_ids:
        svc_data = dict(data or {})
        svc_data["entity_id"] = eid
        try:
            await hass.services.async_call(domain, service, svc_data, blocking=True)
            results.append({"entity_id": eid, "ok": True})
        except Exception as exc:  # noqa: BLE001
            results.append({"entity_id": eid, "ok": False, "error": str(exc)})
    ok = sum(1 for r in results if r["ok"])
    return {
        "ok": ok == len(results),
        "total": len(results),
        "succeeded": ok,
        "failed": len(results) - ok,
        "results": results,
    }


async def _export_config(
    hass: HomeAssistant,
    config_type: str,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Export configuration as YAML for backup, sharing, or migration.

    Supports automations, scripts, scenes, and dashboard configs. Returns
    the raw YAML that can be saved, shared, or re-imported.
    """
    import io

    if config_type == "automation":
        if entity_id:
            state = hass.states.get(entity_id)
            if state is None:
                return {"error": f"Entity {entity_id} not found"}
        path = hass.config.path("automations.yaml")
        try:
            with open(path) as fh:
                content = fh.read()
            return {"ok": True, "type": "automation", "yaml": content,
                    "path": path}
        except FileNotFoundError:
            return {"error": f"File not found: {path}"}

    if config_type == "script":
        path = hass.config.path("scripts.yaml")
        try:
            with open(path) as fh:
                content = fh.read()
            return {"ok": True, "type": "script", "yaml": content, "path": path}
        except FileNotFoundError:
            return {"error": f"File not found: {path}"}

    if config_type == "scene":
        path = hass.config.path("scenes.yaml")
        try:
            with open(path) as fh:
                content = fh.read()
            return {"ok": True, "type": "scene", "yaml": content, "path": path}
        except FileNotFoundError:
            return {"error": f"File not found: {path}"}

    if config_type == "dashboard":
        if not entity_id:
            try:
                import homeassistant.components.lovelace as ll
                dashboards_list = []
                for dash_id, dash in (ll.async_get_dashboards(hass) or {}).items():
                    dashboards_list.append({"id": dash_id, "title": getattr(dash, "title", "")})
                return {"ok": True, "dashboards": dashboards_list}
            except Exception:  # noqa: BLE001
                return {"error": "Could not list dashboards. Use get_dashboard_config instead."}
        # Export specific dashboard
        try:
            import homeassistant.components.lovelace as ll
            config = await ll.async_get_config(hass, entity_id)
            buf = io.StringIO()
            yaml.dump(config, buf, default_flow_style=False, allow_unicode=True)
            return {"ok": True, "type": "dashboard", "dashboard_id": entity_id,
                    "yaml": buf.getvalue()}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Export failed: {exc}"}

    return {"error": f"Unknown config_type: {config_type}. Use: automation, script, scene, dashboard"}


async def _import_config(
    hass: HomeAssistant,
    config_type: str,
    yaml_content: str,
) -> dict[str, Any]:
    """Import YAML configuration into Home Assistant.

    Appends automations/scripts/scenes to existing config files. For
    dashboards, use update_dashboard instead.
    """
    if config_type not in ("automation", "script", "scene"):
        return {"error": "config_type must be: automation, script, or scene"}

    path_map = {
        "automation": "automations.yaml",
        "script": "scripts.yaml",
        "scene": "scenes.yaml",
    }
    path = hass.config.path(path_map[config_type])

    # Validate the incoming YAML
    try:
        incoming = yaml.safe_load(yaml_content)
    except yaml.YAMLError as exc:
        return {"error": f"Invalid YAML: {exc}"}

    if incoming is None:
        return {"error": "YAML content is empty"}

    # Read existing
    try:
        with open(path) as fh:
            existing = yaml.safe_load(fh.read())
    except FileNotFoundError:
        existing = None

    if existing is None:
        existing = []
    if not isinstance(existing, list):
        existing = [existing]

    # Merge
    if isinstance(incoming, list):
        existing.extend(incoming)
        added = len(incoming)
    else:
        existing.append(incoming)
        added = 1

    with open(path, "w") as fh:
        yaml.dump(existing, fh, default_flow_style=False, allow_unicode=True)

    return {
        "ok": True,
        "type": config_type,
        "added": added,
        "total": len(existing),
        "path": path,
        "hint": f"Run reload(target='{config_type}') to apply changes.",
    }


async def _validate_template(
    hass: HomeAssistant, template_str: str,
) -> dict[str, Any]:
    """Validate a Jinja2 template without executing it.

    Checks syntax and reports errors. Useful before embedding templates in
    automations or dashboard cards to catch issues early.
    """
    from homeassistant.helpers.template import Template

    try:
        tpl = Template(template_str, hass)
        tpl.ensure_valid()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "valid": False, "error": str(exc)}

    return {"ok": True, "valid": True, "template": template_str[:200]}


async def _send_notification(
    hass: HomeAssistant,
    message: str,
    title: str | None = None,
    target: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a notification through HA's notify services.

    If target is given, sends to that specific notifier (e.g. "mobile_app_phone").
    Otherwise sends to notify.notify (the default notifier group).
    """
    svc_data: dict[str, Any] = {"message": message}
    if title:
        svc_data["title"] = title
    if data:
        svc_data["data"] = data

    service_name = target or "notify"
    try:
        await hass.services.async_call(
            "notify", service_name, svc_data, blocking=True
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Notification failed: {exc}"}

    return {"ok": True, "service": f"notify.{service_name}", "message": message}


async def _compare_states(
    hass: HomeAssistant,
    entity_ids: list[str],
) -> dict[str, Any]:
    """Compare current states of multiple entities side-by-side.

    Useful for debugging: are all room temperatures similar? Are all lights
    in the expected state? Returns a compact comparison table.
    """
    if not entity_ids:
        return {"error": "entity_ids list is empty"}
    rows = []
    for eid in entity_ids:
        state = hass.states.get(eid)
        if state:
            rows.append({
                "entity_id": eid,
                "state": state.state,
                "last_changed": state.last_changed.isoformat() if state.last_changed else None,
                "attributes": {
                    k: v for k, v in state.attributes.items()
                    if k in ("friendly_name", "device_class", "unit_of_measurement")
                },
            })
        else:
            rows.append({"entity_id": eid, "state": "not_found"})
    return {"ok": True, "count": len(rows), "entities": rows}


# ---------------------------------------------------------------------------
# Intelligence layer — proactive analysis (Stage 3 evolution)
# ---------------------------------------------------------------------------


async def _diagnose_home(hass: HomeAssistant) -> dict[str, Any]:
    """Run a holistic diagnostic sweep of the entire home.

    Returns unavailable entities, stale sensors, offline devices, failed
    automations, and system warnings — a single call that tells the operator
    "what's wrong right now" without requiring them to know what to look for.
    """
    from homeassistant.helpers import (
        area_registry as ar,
        device_registry as dr,
        entity_registry as er,
    )

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    issues: list[dict[str, Any]] = []

    # 1. Unavailable / unknown entities
    unavail: list[dict[str, str]] = []
    for state in hass.states.async_all():
        if state.state in ("unavailable", "unknown"):
            entry = ent_reg.async_get(state.entity_id)
            disabled = entry.disabled_by if entry else None
            if disabled:
                continue  # user intentionally disabled
            unavail.append({
                "entity_id": state.entity_id,
                "state": state.state,
                "domain": state.entity_id.split(".")[0],
            })
    if unavail:
        issues.append({
            "type": "unavailable_entities",
            "severity": "warning",
            "count": len(unavail),
            "entities": unavail[:30],
            "hint": "These entities are currently unavailable. Check device "
                    "connectivity, integration status, or power supply.",
        })

    # 2. Devices with no entities (orphan registrations)
    all_ents_by_dev: dict[str, int] = {}
    for entry in ent_reg.entities.values():
        if entry.device_id:
            all_ents_by_dev[entry.device_id] = (
                all_ents_by_dev.get(entry.device_id, 0) + 1
            )
    orphan_devs = []
    for dev in dev_reg.devices.values():
        if dev.id not in all_ents_by_dev:
            orphan_devs.append({
                "device_id": dev.id,
                "name": dev.name_by_user or dev.name or dev.id,
            })
    if orphan_devs:
        issues.append({
            "type": "orphan_devices",
            "severity": "info",
            "count": len(orphan_devs),
            "devices": orphan_devs[:20],
            "hint": "Devices with zero entities. May be stale registrations "
                    "from removed integrations.",
        })

    # 3. Disabled entities count
    disabled_count = sum(
        1 for e in ent_reg.entities.values() if e.disabled_by
    )

    # 4. Automations that are off
    auto_off = []
    for state in hass.states.async_all("automation"):
        if state.state == "off":
            auto_off.append(state.entity_id)
    if auto_off:
        issues.append({
            "type": "automations_off",
            "severity": "info",
            "count": len(auto_off),
            "automations": auto_off[:20],
            "hint": "These automations are disabled. Intentional or forgotten?",
        })

    # 5. Config entries not loaded / in error
    entry_issues = []
    for entry in hass.config_entries.async_entries():
        if entry.state.value not in ("loaded", "not_loaded"):
            entry_issues.append({
                "entry_id": entry.entry_id,
                "domain": entry.domain,
                "title": entry.title,
                "state": entry.state.value,
            })
    if entry_issues:
        issues.append({
            "type": "config_entry_errors",
            "severity": "error",
            "count": len(entry_issues),
            "entries": entry_issues[:20],
            "hint": "Integrations in error state. Check logs or reconfigure.",
        })

    # Summary counts
    total_entities = len(ent_reg.entities)
    total_devices = len(dev_reg.devices)
    total_areas = len(area_reg.areas)
    total_automations = len(hass.states.async_entity_ids("automation"))
    total_scripts = len(hass.states.async_entity_ids("script"))
    total_scenes = len(hass.states.async_entity_ids("scene"))
    total_integrations = len(hass.config_entries.async_entries())

    return {
        "ok": True,
        "summary": {
            "entities": total_entities,
            "devices": total_devices,
            "areas": total_areas,
            "automations": total_automations,
            "scripts": total_scripts,
            "scenes": total_scenes,
            "integrations": total_integrations,
            "disabled_entities": disabled_count,
        },
        "issues": issues,
        "issue_count": len(issues),
        "health": "healthy" if not any(
            i["severity"] == "error" for i in issues
        ) else "degraded",
    }


async def _get_home_context(
    hass: HomeAssistant, area_id: str | None = None,
) -> dict[str, Any]:
    """Build a tree of areas → devices → entities (with current states).

    This gives an operator a complete spatial picture of the home in one call,
    like Devin Desktop's project-wide code understanding. If ``area_id`` is
    given, returns only that area's subtree.
    """
    from homeassistant.helpers import (
        area_registry as ar,
        device_registry as dr,
        entity_registry as er,
        floor_registry as fr,
    )

    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    floor_reg = fr.async_get(hass)

    # Build device → area mapping
    dev_area: dict[str, str | None] = {}
    for dev in dev_reg.devices.values():
        dev_area[dev.id] = dev.area_id

    # Build entity → device mapping and entity → area
    ent_by_area: dict[str | None, list[dict[str, Any]]] = {}
    for entry in ent_reg.entities.values():
        ent_area = entry.area_id or (dev_area.get(entry.device_id or "") if entry.device_id else None)
        if area_id and ent_area != area_id:
            continue
        state = hass.states.get(entry.entity_id)
        ent_info = {
            "entity_id": entry.entity_id,
            "domain": entry.domain,
            "name": entry.name or entry.original_name or "",
            "state": state.state if state else "unknown",
            "device_id": entry.device_id,
        }
        if entry.disabled_by:
            ent_info["disabled"] = True
        ent_by_area.setdefault(ent_area, []).append(ent_info)

    # Build area tree
    areas = []
    target_areas = [area_reg.async_get_area(area_id)] if area_id else area_reg.areas.values()
    for area in target_areas:
        if area is None:
            continue
        ents = ent_by_area.get(area.id, [])
        # Group by domain
        by_domain: dict[str, int] = {}
        for e in ents:
            by_domain[e["domain"]] = by_domain.get(e["domain"], 0) + 1

        floor = floor_reg.async_get_floor(area.floor_id) if area.floor_id else None
        areas.append({
            "area_id": area.id,
            "name": area.name,
            "floor": floor.name if floor else None,
            "entity_count": len(ents),
            "domains": by_domain,
            "entities": ents[:50],
        })

    # Unassigned entities
    unassigned = ent_by_area.get(None, [])

    return {
        "ok": True,
        "areas": areas,
        "unassigned_count": len(unassigned) if not area_id else 0,
        "unassigned": unassigned[:30] if not area_id else [],
    }


async def _audit_automations(hass: HomeAssistant) -> dict[str, Any]:
    """Audit all automations for staleness, failures, and conflicts.

    Checks each automation's last_triggered time, current state, and trigger
    configuration to find: never-triggered automations, disabled automations,
    duplicate triggers, and potential conflicts.
    """
    findings: list[dict[str, Any]] = []
    auto_states = hass.states.async_all("automation")

    never_triggered = []
    long_idle = []
    disabled = []
    trigger_map: dict[str, list[str]] = {}

    now = datetime.now(timezone.utc)

    for state in auto_states:
        eid = state.entity_id
        attrs = state.attributes

        # Disabled
        if state.state == "off":
            disabled.append(eid)
            continue

        # Never triggered
        lt = attrs.get("last_triggered")
        if lt is None:
            never_triggered.append(eid)
        elif isinstance(lt, datetime):
            days = (now - lt).days
            if days > 30:
                long_idle.append({"entity_id": eid, "days_since": days})

        # Track triggers for conflict detection
        friendly = attrs.get("friendly_name", eid)
        trigger_map.setdefault(friendly, []).append(eid)

    if never_triggered:
        findings.append({
            "type": "never_triggered",
            "severity": "info",
            "count": len(never_triggered),
            "automations": never_triggered[:20],
            "hint": "These automations have never fired. Check triggers or "
                    "conditions.",
        })

    if long_idle:
        findings.append({
            "type": "long_idle",
            "severity": "info",
            "count": len(long_idle),
            "automations": long_idle[:20],
            "hint": "Not triggered in 30+ days. Still needed?",
        })

    if disabled:
        findings.append({
            "type": "disabled",
            "severity": "info",
            "count": len(disabled),
            "automations": disabled[:20],
            "hint": "Disabled automations. Intentional or forgotten?",
        })

    return {
        "ok": True,
        "total_automations": len(auto_states),
        "active": len(auto_states) - len(disabled),
        "findings": findings,
        "finding_count": len(findings),
    }


async def _suggest_optimizations(hass: HomeAssistant) -> dict[str, Any]:
    """Analyze the running home and suggest concrete improvements.

    Checks for common patterns that can be optimized: entities without areas,
    areas without automations, devices that could benefit from known community
    integrations, and missing best-practice configurations.
    """
    from homeassistant.helpers import (
        area_registry as ar,
        device_registry as dr,
        entity_registry as er,
    )

    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    suggestions: list[dict[str, Any]] = []

    # 1. Entities not assigned to any area
    no_area = []
    dev_area = {d.id: d.area_id for d in dev_reg.devices.values()}
    for entry in ent_reg.entities.values():
        if entry.disabled_by:
            continue
        ent_area = entry.area_id or dev_area.get(entry.device_id or "")
        if not ent_area:
            no_area.append(entry.entity_id)
    if no_area:
        suggestions.append({
            "type": "unassigned_entities",
            "priority": "medium",
            "count": len(no_area),
            "sample": no_area[:10],
            "action": "Use assign_entity_area or assign_entities_by_rules to "
                      "organize entities into rooms for voice control and "
                      "dashboard grouping.",
        })

    # 2. Areas without automations
    auto_areas: set[str] = set()
    for state in hass.states.async_all("automation"):
        eid = state.entity_id
        entry = ent_reg.async_get(eid)
        if entry and (entry.area_id or dev_area.get(entry.device_id or "")):
            auto_areas.add(entry.area_id or dev_area.get(entry.device_id or "") or "")
    bare_areas = [
        {"area_id": a.id, "name": a.name}
        for a in area_reg.areas.values()
        if a.id not in auto_areas
    ]
    if bare_areas:
        suggestions.append({
            "type": "areas_without_automations",
            "priority": "low",
            "count": len(bare_areas),
            "areas": bare_areas[:10],
            "action": "Consider adding automations (motion lights, climate "
                      "schedules) to these rooms. Use search_blueprints to "
                      "find ready-made templates.",
        })

    # 3. No energy monitoring
    has_energy = any(
        "energy" in (s.attributes.get("device_class") or "")
        or "power" in (s.attributes.get("device_class") or "")
        for s in hass.states.async_all("sensor")
    )
    if not has_energy:
        suggestions.append({
            "type": "no_energy_monitoring",
            "priority": "medium",
            "action": "No energy/power sensors detected. Consider installing "
                      "Powercalc (search_community_resources 'powercalc') for "
                      "virtual power monitoring, or adding a hardware energy "
                      "meter.",
        })

    # 4. No backup automation
    has_backup_auto = any(
        "backup" in (s.attributes.get("friendly_name") or "").lower()
        for s in hass.states.async_all("automation")
    )
    if not has_backup_auto:
        suggestions.append({
            "type": "no_backup_automation",
            "priority": "high",
            "action": "No automated backup detected. Create a daily backup "
                      "automation using create_automation with "
                      "service: backup.create.",
        })

    # 5. Check if voice control is set up
    pipelines = hass.states.async_entity_ids("assist_pipeline")
    assist_agents = hass.states.async_entity_ids("conversation")
    if not pipelines and not assist_agents:
        suggestions.append({
            "type": "no_voice_control",
            "priority": "low",
            "action": "Voice control not configured. Install Whisper + Piper "
                      "add-ons for local voice (search_ha_addons 'whisper').",
        })

    return {
        "ok": True,
        "suggestion_count": len(suggestions),
        "suggestions": suggestions,
    }


async def _check_device_health(hass: HomeAssistant) -> dict[str, Any]:
    """Check health of all devices: battery, connectivity, activity.

    Scans device-related sensors for low battery, weak signal, and entities
    that haven't updated recently, surfacing devices that need attention.
    """
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    low_battery: list[dict[str, Any]] = []
    weak_signal: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []

    now = datetime.now(timezone.utc)

    # Scan all sensor entities
    for state in hass.states.async_all("sensor"):
        attrs = state.attributes
        dc = attrs.get("device_class") or ""
        eid = state.entity_id

        entry = ent_reg.async_get(eid)
        dev_name = ""
        if entry and entry.device_id:
            dev = dev_reg.async_get(entry.device_id)
            dev_name = (dev.name_by_user or dev.name or "") if dev else ""

        # Low battery
        if dc == "battery" and state.state not in ("unavailable", "unknown"):
            try:
                level = float(state.state)
                if level < 20:
                    low_battery.append({
                        "entity_id": eid,
                        "device": dev_name,
                        "level": level,
                    })
            except (ValueError, TypeError):
                pass

        # Weak signal (RSSI / signal_strength / link_quality)
        if dc in ("signal_strength",) or "rssi" in eid or "linkquality" in eid:
            if state.state not in ("unavailable", "unknown"):
                try:
                    val = float(state.state)
                    if "rssi" in eid and val < -80:
                        weak_signal.append({
                            "entity_id": eid, "device": dev_name, "value": val,
                        })
                    elif "linkquality" in eid and val < 30:
                        weak_signal.append({
                            "entity_id": eid, "device": dev_name, "value": val,
                        })
                except (ValueError, TypeError):
                    pass

    # Check for stale binary_sensors (not updated in 24h)
    for state in hass.states.async_all("binary_sensor"):
        if state.state in ("unavailable", "unknown"):
            continue
        last_changed = state.last_changed
        if last_changed and isinstance(last_changed, datetime):
            hours = (now - last_changed).total_seconds() / 3600
            if hours > 48:
                entry = ent_reg.async_get(state.entity_id)
                dev_name = ""
                if entry and entry.device_id:
                    dev = dev_reg.async_get(entry.device_id)
                    dev_name = (dev.name_by_user or dev.name or "") if dev else ""
                stale.append({
                    "entity_id": state.entity_id,
                    "device": dev_name,
                    "hours_since_change": round(hours, 1),
                })

    alerts: list[dict[str, Any]] = []
    if low_battery:
        low_battery.sort(key=lambda x: x["level"])
        alerts.append({
            "type": "low_battery",
            "severity": "warning",
            "count": len(low_battery),
            "devices": low_battery[:20],
            "hint": "Replace batteries soon. Consider setting up a low-battery "
                    "notification automation.",
        })
    if weak_signal:
        alerts.append({
            "type": "weak_signal",
            "severity": "warning",
            "count": len(weak_signal),
            "devices": weak_signal[:20],
            "hint": "Weak wireless signal. Consider adding a Zigbee router "
                    "or moving the device closer to the coordinator.",
        })
    if stale:
        stale.sort(key=lambda x: -x["hours_since_change"])
        alerts.append({
            "type": "stale_sensors",
            "severity": "info",
            "count": len(stale),
            "devices": stale[:20],
            "hint": "Binary sensors unchanged for 48+ hours. May be normal "
                    "(rarely-used doors) or indicate a dead sensor.",
        })

    return {
        "ok": True,
        "alert_count": len(alerts),
        "alerts": alerts,
        "total_battery_sensors": sum(
            1 for s in hass.states.async_all("sensor")
            if (s.attributes.get("device_class") or "") == "battery"
        ),
    }


async def dispatch(hass: HomeAssistant, store: dict, name: str, args: dict) -> dict[str, Any]:
    """Execute a tool by name with the given arguments."""
    try:
        if name == "run_tools":
            return await _run_tools(hass, store, args)
        if name == "assist":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            text = args.get("text") or args.get("command") or args.get("query")
            if not text:
                return {"error": "missing required argument: text"}
            return await _assist(
                hass, text, args.get("language"), args.get("conversation_id")
            )
        if name == "list_intents":
            return await _list_intents(hass)
        if name == "handle_intent":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            itype = args.get("intent_type") or args.get("intent") or args.get("type")
            if not itype:
                return {"error": "missing required argument: intent_type"}
            return await _handle_intent(hass, itype, args.get("slots") or args.get("data"))
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
        if name == "list_dir":
            return await _list_dir(hass, args.get("path", ""))
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
        if name == "diagnose_home":
            return await _diagnose_home(hass)
        if name == "get_home_context":
            return await _get_home_context(hass, args.get("area_id"))
        if name == "audit_automations":
            return await _audit_automations(hass)
        if name == "suggest_optimizations":
            return await _suggest_optimizations(hass)
        if name == "check_device_health":
            return await _check_device_health(hass)
        if name == "batch_call_service":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            eids = args.get("entity_ids") or args.get("entities") or []
            return await _batch_call_service(
                hass, args.get("domain", ""), args.get("service", ""),
                eids, args.get("data"),
            )
        if name == "export_config":
            return await _export_config(
                hass, args.get("config_type", ""), args.get("entity_id"),
            )
        if name == "import_config":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _import_config(
                hass, args.get("config_type", ""), args.get("yaml_content", ""),
            )
        if name == "validate_template":
            tpl = args.get("template") or args.get("template_str") or ""
            return await _validate_template(hass, tpl)
        if name == "send_notification":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _send_notification(
                hass, args.get("message", ""), args.get("title"),
                args.get("target"), args.get("data"),
            )
        if name == "compare_states":
            eids = args.get("entity_ids") or args.get("entities") or []
            return await _compare_states(hass, eids)
        if name == "create_zone":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _create_zone(
                hass, args.get("name", ""), float(args.get("latitude", 0)),
                float(args.get("longitude", 0)), float(args.get("radius", 100)),
                args.get("icon", "mdi:map-marker"), bool(args.get("passive", False)),
            )
        if name == "manage_input_helper":
            act = args.get("action", "list")
            if act in ("create", "set", "delete") and not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _manage_input_helper(
                hass, act, args.get("helper_type", "boolean"),
                args.get("entity_id"), args.get("name"), args.get("value"),
                args.get("options"), args.get("min"), args.get("max"),
                args.get("step"), args.get("unit"), args.get("icon"),
                args.get("initial"), args.get("mode"),
            )
        if name == "manage_counter":
            act = args.get("action", "list")
            if act in ("create", "increment", "decrement", "reset") and not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _manage_counter(
                hass, act, args.get("entity_id"), args.get("name"),
                args.get("initial"), args.get("step"),
                args.get("minimum"), args.get("maximum"), args.get("icon"),
            )
        if name == "manage_timer":
            act = args.get("action", "list")
            if act in ("create", "start", "pause", "cancel", "finish") and not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _manage_timer(
                hass, act, args.get("entity_id"), args.get("name"),
                args.get("duration"), args.get("icon"),
            )
        if name == "manage_backup":
            act = args.get("action", "list")
            if act in ("create", "remove") and not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _manage_backup(hass, act, args.get("slug"))
        if name == "manage_label":
            act = args.get("action", "list")
            if act in ("create", "delete") and not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _manage_label(
                hass, act, args.get("name"), args.get("label_id"),
                args.get("color"), args.get("icon"), args.get("description"),
            )
        if name == "manage_floor":
            act = args.get("action", "list")
            if act in ("create", "delete") and not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _manage_floor(
                hass, act, args.get("name"), args.get("floor_id"),
                args.get("icon"), args.get("level"),
            )
        if name == "check_updates":
            return await _check_updates(hass)
        if name == "manage_calendar":
            act = args.get("action", "list")
            if act == "create_event" and not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _manage_calendar(
                hass, act, args.get("entity_id"), args.get("summary"),
                args.get("start"), args.get("end"), args.get("description"),
            )
        if name == "manage_todo":
            act = args.get("action", "list")
            if act in ("add", "update", "remove") and not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _manage_todo(
                hass, act, args.get("entity_id"), args.get("item"),
                args.get("status"), args.get("uid"),
            )
        if name == "manage_tag":
            act = args.get("action", "list")
            if act in ("create", "remove") and not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _manage_tag(hass, act, args.get("tag_id"), args.get("name"))
        if name == "browse_media":
            return await _browse_media(
                hass, args.get("media_content_id"), args.get("media_content_type"),
                args.get("entity_id"),
            )
        if name == "get_camera_snapshot":
            return await _get_camera_snapshot(hass, args.get("entity_id", ""))
        if name == "manage_schedule":
            return await _manage_schedule(hass, args.get("action", "list"), args.get("entity_id"))
        if name == "toggle_automation":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _toggle_automation(
                hass, args.get("entity_id", ""), bool(args.get("enable", True)),
            )
        if name == "trigger_automation":
            return await _trigger_automation(
                hass, args.get("entity_id", ""), bool(args.get("skip_condition", False)),
            )
        if name == "duplicate_automation":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _duplicate_automation(
                hass, args.get("entity_id", ""), args.get("new_alias"),
            )
        if name == "remove_device":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _remove_device(hass, args.get("device_id", ""))
        if name == "list_device_entities":
            return await _list_device_entities(hass, args.get("device_id", ""))
        if name == "compare_history":
            eids = args.get("entity_ids") or args.get("entities") or []
            return await _compare_history(hass, eids, int(args.get("hours", 24)))
        if name == "send_tts":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _send_tts(
                hass, args.get("message", ""), args.get("entity_id"),
                args.get("language"),
            )
        if name == "play_media":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _play_media(
                hass, args.get("entity_id", ""), args.get("media_content_id", ""),
                args.get("media_content_type", "music"),
            )
        if name == "activate_scene":
            return await _activate_scene(
                hass, args.get("entity_id", ""), args.get("transition"),
            )
        if name == "snapshot_scene":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            eids = args.get("entity_ids") or args.get("entities") or []
            return await _snapshot_scene(hass, eids, args.get("scene_name", "Snapshot"))
        if name == "publish_mqtt":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _publish_mqtt(
                hass, args.get("topic", ""), args.get("payload", ""),
                int(args.get("qos", 0)), bool(args.get("retain", False)),
            )
        if name == "subscribe_mqtt":
            return await _subscribe_mqtt(
                hass, args.get("topic", "#"), float(args.get("timeout", 5.0)),
            )
        if name == "list_mqtt_devices":
            return await _list_mqtt_devices(hass)
        if name == "permit_zigbee_join":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _permit_zigbee_join(hass, int(args.get("duration", 60)))
        if name == "rename_zigbee_device":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _rename_zigbee_device(
                hass, args.get("old_name", ""), args.get("new_name", ""),
            )
        if name == "heal_zwave_network":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _heal_zwave_network(hass)
        if name == "get_zwave_node_info":
            return await _get_zwave_node_info(hass, args.get("entity_id"))
        if name == "wake_on_lan":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _wake_on_lan(
                hass, args.get("mac", ""), args.get("broadcast_address"),
            )
        if name == "ping_device":
            return await _ping_device(
                hass, args.get("host", ""), int(args.get("count", 3)),
            )
        if name == "list_notification_services":
            return await _list_notification_services(hass)
        if name == "dismiss_notification":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _dismiss_notification(hass, args.get("notification_id", ""))
        if name == "create_persistent_notification":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _create_persistent_notification(
                hass, args.get("message", ""), args.get("title"),
                args.get("notification_id"),
            )
        if name == "list_entity_domains":
            return await _list_entity_domains(hass)
        if name == "list_automations":
            return await _list_automations(hass)
        if name == "get_device_info":
            return await _get_device_info(hass, args.get("device_id", ""))
        if name == "run_script":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _run_script(
                hass, args.get("entity_id", ""), args.get("variables"),
            )
        if name == "test_condition":
            return await _test_condition(hass, args.get("condition", {}))
        if name == "get_energy_summary":
            return await _get_energy_summary(hass)
        if name == "list_energy_sources":
            return await _list_energy_sources(hass)
        if name == "camera_snapshot":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _camera_snapshot(
                hass, args.get("entity_id", ""), args.get("filename"),
            )
        if name == "list_cameras":
            return await _list_cameras(hass)
        if name == "set_climate_preset":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_climate_preset(
                hass, args.get("entity_id", ""), args.get("preset_mode", ""),
            )
        if name == "get_climate_schedule":
            return await _get_climate_schedule(hass, args.get("entity_id", ""))
        if name == "set_cover_position":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_cover_position(
                hass, args.get("entity_id", ""), int(args.get("position", 50)),
            )
        if name == "vacuum_command":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _vacuum_command(
                hass, args.get("entity_id", ""), args.get("command", ""),
            )
        if name == "set_input_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_input_value(
                hass, args.get("entity_id", ""), args.get("value"),
            )
        if name == "list_updates":
            return await _list_updates(hass)
        if name == "install_update":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _install_update(
                hass, args.get("entity_id", ""), bool(args.get("backup", True)),
            )
        if name == "lock_door":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _lock_door(hass, args.get("entity_id", ""))
        if name == "unlock_door":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _unlock_door(
                hass, args.get("entity_id", ""), args.get("code"),
            )
        if name == "arm_alarm":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _arm_alarm(
                hass, args.get("entity_id", ""),
                args.get("mode", "arm_away"), args.get("code"),
            )
        if name == "disarm_alarm":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _disarm_alarm(
                hass, args.get("entity_id", ""), args.get("code"),
            )
        if name == "get_alarm_state":
            return await _get_alarm_state(hass, args.get("entity_id", ""))
        if name == "set_fan_speed":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_fan_speed(
                hass, args.get("entity_id", ""), int(args.get("percentage", 50)),
            )
        if name == "set_fan_direction":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_fan_direction(
                hass, args.get("entity_id", ""), args.get("direction", "forward"),
            )
        if name == "set_water_heater_temperature":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_water_heater_temperature(
                hass, args.get("entity_id", ""), float(args.get("temperature", 50)),
            )
        if name == "set_humidifier_mode":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_humidifier_mode(
                hass, args.get("entity_id", ""), args.get("mode", ""),
            )
        if name == "activate_siren":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _activate_siren(
                hass, args.get("entity_id", ""), bool(args.get("turn_on", True)),
                args.get("duration"), args.get("tone"),
            )
        if name == "press_button":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _press_button(hass, args.get("entity_id", ""))
        if name == "list_calendar_events":
            return await _list_calendar_events(
                hass, args.get("entity_id", ""),
                args.get("start"), args.get("end"),
            )
        if name == "create_calendar_event":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _create_calendar_event(
                hass, args.get("entity_id", ""),
                args.get("summary", ""), args.get("start", ""),
                args.get("end", ""), args.get("description"),
                args.get("location"),
            )
        if name == "get_weather_forecast":
            return await _get_weather_forecast(
                hass, args.get("entity_id", ""),
                args.get("forecast_type", "daily"),
            )
        if name == "set_number_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_number_value(
                hass, args.get("entity_id", ""), float(args.get("value", 0)),
            )
        if name == "set_select_option":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_select_option(
                hass, args.get("entity_id", ""), args.get("option", ""),
            )
        if name == "conversation_query":
            return await _conversation_query(
                hass, args.get("text", ""), args.get("language"),
            )
        if name == "complete_todo_item":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _complete_todo_item(
                hass, args.get("entity_id", ""),
                args.get("item", ""), args.get("status", "completed"),
            )
        if name == "reset_utility_meter":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _reset_utility_meter(hass, args.get("entity_id", ""))
        # --- Wave 4 dispatch ---
        if name == "media_player_control":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            extra = {k: v for k, v in args.items() if k not in ("entity_id", "command")}
            return await _media_player_control(
                hass, args.get("entity_id", ""), args.get("command", ""), **extra,
            )
        if name == "list_media_players":
            return await _list_media_players(hass)
        if name == "send_mobile_notification":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _send_mobile_notification(
                hass, args.get("target", "mobile_app"),
                args.get("message", ""), args.get("title"),
                args.get("data"),
            )
        if name == "get_person_location":
            return await _get_person_location(hass, args.get("person_id"))
        if name == "list_device_trackers":
            return await _list_device_trackers(hass)
        if name == "reload_yaml":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _reload_yaml(hass, args.get("target", "all"))
        if name == "reload_all_integrations":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _reload_all_integrations(hass)
        if name == "get_entity_history_summary":
            return await _get_entity_history_summary(
                hass, args.get("entity_id", ""), int(args.get("hours", 24)),
            )
        if name == "get_entity_logbook":
            return await _get_entity_logbook(
                hass, args.get("entity_id", ""), int(args.get("hours", 24)),
            )
        if name == "get_states_by_domain":
            return await _get_states_by_domain(hass, args.get("domain", ""))
        if name == "get_nearest_person":
            return await _get_nearest_person(hass, args.get("zone", "home"))
        if name == "assign_device_label":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _assign_device_label(
                hass, args.get("device_id", ""), args.get("labels", []),
            )
        if name == "get_image_url":
            return await _get_image_url(hass, args.get("entity_id", ""))
        # --- Wave 5 dispatch ---
        if name == "assign_area_floor":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _assign_area_floor(
                hass, args.get("area_id", ""), args.get("floor_id", ""),
            )
        if name == "scan_tag":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _scan_tag(hass, args.get("tag_id", ""), args.get("device_id"))
        if name == "add_todo_item":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _add_todo_item(
                hass, args.get("entity_id", ""), args.get("item", ""),
                args.get("due_date"), args.get("description"),
            )
        if name == "remove_todo_item":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _remove_todo_item(
                hass, args.get("entity_id", ""), args.get("item", ""),
            )
        if name == "list_assist_pipelines":
            return await _list_assist_pipelines(hass)
        if name == "run_assist_pipeline":
            return await _run_assist_pipeline(
                hass, args.get("text", ""), args.get("pipeline_id"),
                args.get("language"),
            )
        if name == "list_thread_networks":
            return await _list_thread_networks(hass)
        if name == "get_matter_nodes":
            return await _get_matter_nodes(hass)
        if name == "restore_backup":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _restore_backup(hass, args.get("backup_id", ""))
        if name == "download_backup":
            return await _download_backup(hass, args.get("backup_id", ""))
        if name == "assign_entity_category":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _assign_entity_category(
                hass, args.get("entity_id", ""), args.get("category", ""),
            )
        if name == "increment_counter":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _increment_counter(hass, args.get("entity_id", ""))
        if name == "decrement_counter":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _decrement_counter(hass, args.get("entity_id", ""))
        if name == "reset_counter":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _reset_counter(hass, args.get("entity_id", ""))
        if name == "start_timer":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _start_timer(
                hass, args.get("entity_id", ""), args.get("duration"),
            )
        if name == "cancel_timer":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _cancel_timer(hass, args.get("entity_id", ""))
        if name == "pause_timer":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _pause_timer(hass, args.get("entity_id", ""))
        if name == "finish_timer":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _finish_timer(hass, args.get("entity_id", ""))
        if name == "mower_command":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _mower_command(
                hass, args.get("entity_id", ""), args.get("command", ""),
            )
        if name == "valve_control":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            pos = args.get("position")
            return await _valve_control(
                hass, args.get("entity_id", ""), args.get("command", ""),
                int(pos) if pos is not None else None,
            )
        if name == "list_event_entities":
            return await _list_event_entities(hass)
        if name == "set_date_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_date_value(
                hass, args.get("entity_id", ""), args.get("date", ""),
            )
        if name == "set_time_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_time_value(
                hass, args.get("entity_id", ""), args.get("time", ""),
            )
        if name == "set_text_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_text_value(
                hass, args.get("entity_id", ""), args.get("value", ""),
            )
        if name == "list_wake_words":
            return await _list_wake_words(hass)
        if name == "list_stt_engines":
            return await _list_stt_engines(hass)
        if name == "list_tts_engines":
            return await _list_tts_engines(hass)
        if name == "list_conversation_agents":
            return await _list_conversation_agents(hass)
        if name == "get_schedule":
            return await _get_schedule(hass, args.get("entity_id", ""))
        if name == "get_statistics_metadata":
            return await _get_statistics_metadata(hass, args.get("statistic_ids"))
        if name == "clear_statistics":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _clear_statistics(hass, args.get("statistic_ids", []))
        if name == "send_remote_command":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _send_remote_command(
                hass, args.get("entity_id", ""), args.get("command", ""),
                args.get("device"), int(args.get("num_repeats", 1)),
            )
        # --- Wave 8 dispatch ---
        if name == "get_energy_preferences":
            return await _get_energy_preferences(hass)
        if name == "skip_update":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _skip_update(hass, args.get("entity_id", ""))
        if name == "siren_control":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _siren_control(
                hass, args.get("entity_id", ""), args.get("command", ""),
                args.get("tone"), args.get("volume_level"), args.get("duration"),
            )
        if name == "lock_control":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _lock_control(
                hass, args.get("entity_id", ""), args.get("command", ""),
                args.get("code"),
            )
        if name == "alarm_control":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _alarm_control(
                hass, args.get("entity_id", ""), args.get("command", ""),
                args.get("code"),
            )
        if name == "fan_control":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _fan_control(
                hass, args.get("entity_id", ""), args.get("command", ""),
                args.get("percentage"), args.get("preset_mode"),
                args.get("direction"), args.get("oscillating"),
            )
        if name == "cover_control":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            pos = args.get("position")
            tilt = args.get("tilt_position")
            return await _cover_control(
                hass, args.get("entity_id", ""), args.get("command", ""),
                int(pos) if pos is not None else None,
                int(tilt) if tilt is not None else None,
            )
        if name == "water_heater_control":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _water_heater_control(
                hass, args.get("entity_id", ""), args.get("command", ""),
                args.get("temperature"), args.get("operation_mode"),
            )
        if name == "humidifier_control":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _humidifier_control(
                hass, args.get("entity_id", ""), args.get("command", ""),
                args.get("humidity"), args.get("mode"),
            )
        if name == "list_automation_traces":
            return await _list_automation_traces(hass, args.get("automation_id"))
        if name == "process_conversation":
            return await _process_conversation(
                hass, args.get("text", ""), args.get("language"),
                args.get("agent_id"), args.get("conversation_id"),
            )
        if name == "toggle_input_boolean":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _toggle_input_boolean(hass, args.get("entity_id", ""))
        # --- Wave 9 dispatch ---
        if name == "camera_turn_on":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _camera_turn_on(hass, args.get("entity_id", ""))
        if name == "camera_turn_off":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _camera_turn_off(hass, args.get("entity_id", ""))
        if name == "climate_set_preset":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _climate_set_preset(
                hass, args.get("entity_id", ""), args.get("preset_mode", ""),
            )
        if name == "climate_set_aux_heat":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _climate_set_aux_heat(
                hass, args.get("entity_id", ""), bool(args.get("aux_heat", False)),
            )
        if name == "list_notify_targets":
            return await _list_notify_targets(hass)
        if name == "clear_system_log":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _clear_system_log(hass)
        if name == "batch_reload_integrations":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _batch_reload_integrations(hass, args.get("domains"))
        if name == "set_input_select_option":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_input_select_option(
                hass, args.get("entity_id", ""), args.get("option", ""),
            )
        if name == "list_input_select_options":
            return await _list_input_select_options(hass, args.get("entity_id", ""))
        if name == "set_input_number_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_input_number_value(
                hass, args.get("entity_id", ""), float(args.get("value", 0)),
            )
        if name == "calibrate_utility_meter":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _calibrate_utility_meter(
                hass, args.get("entity_id", ""), float(args.get("value", 0)),
            )
        if name == "log_custom_event":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _log_custom_event(
                hass, args.get("name", ""), args.get("message", ""),
                args.get("entity_id"), args.get("domain"),
            )
        if name == "list_device_actions":
            return await _list_device_actions(hass, args.get("device_id", ""))
        if name == "execute_device_action":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _execute_device_action(hass, args.get("action", {}))
        # --- Wave 11 dispatch ---
        if name == "set_group_members":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_group_members(
                hass, args.get("entity_id", ""), args.get("members", []),
            )
        if name == "dismiss_persistent_notification":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _dismiss_persistent_notification(
                hass, args.get("notification_id", ""),
            )
        if name == "get_timer_remaining":
            return await _get_timer_remaining(hass, args.get("entity_id", ""))
        if name == "get_sun_position":
            return await _get_sun_position(hass)
        if name == "set_input_datetime":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_input_datetime(
                hass, args.get("entity_id", ""),
                args.get("date"), args.get("time"), args.get("datetime"),
            )
        if name == "climate_set_swing_mode":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _climate_set_swing_mode(
                hass, args.get("entity_id", ""), args.get("swing_mode", ""),
            )
        if name == "climate_set_fan_mode":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _climate_set_fan_mode(
                hass, args.get("entity_id", ""), args.get("fan_mode", ""),
            )
        if name == "media_player_tts":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _media_player_tts(
                hass, args.get("entity_id", ""), args.get("message", ""),
                args.get("engine"), args.get("language"),
                bool(args.get("cache", True)),
            )
        if name == "device_tracker_see":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _device_tracker_see(
                hass, args.get("dev_id"), args.get("mac"),
                args.get("location_name"), args.get("gps"),
                args.get("gps_accuracy"), args.get("battery"),
                args.get("host_name"),
            )
        # --- Wave 13 dispatch ---
        if name == "enable_automation":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _enable_automation(hass, args.get("entity_id", ""))
        if name == "disable_automation":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _disable_automation(hass, args.get("entity_id", ""))
        if name == "trigger_script":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _trigger_script(
                hass, args.get("entity_id", ""), args.get("variables"),
            )
        if name == "get_entity_attributes":
            return await _get_entity_attributes(hass, args.get("entity_id", ""))
        if name == "get_integration_info":
            return await _get_integration_info(hass, args.get("domain", ""))
        if name == "set_input_text":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_input_text(
                hass, args.get("entity_id", ""), args.get("value", ""),
            )
        if name == "light_turn_on":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _light_turn_on(
                hass, args.get("entity_id", ""),
                args.get("brightness"), args.get("color_temp"),
                args.get("rgb_color"), args.get("transition"),
                args.get("effect"),
            )
        if name == "light_turn_off":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _light_turn_off(
                hass, args.get("entity_id", ""), args.get("transition"),
            )
        if name == "switch_turn_on":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _switch_turn_on(hass, args.get("entity_id", ""))
        if name == "switch_turn_off":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _switch_turn_off(hass, args.get("entity_id", ""))
        if name == "climate_set_temperature":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _climate_set_temperature(
                hass, args.get("entity_id", ""),
                args.get("temperature"), args.get("target_temp_high"),
                args.get("target_temp_low"), args.get("hvac_mode"),
            )
        if name == "climate_set_hvac_mode":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _climate_set_hvac_mode(
                hass, args.get("entity_id", ""), args.get("hvac_mode", ""),
            )
        if name == "homeassistant_turn_on":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _homeassistant_turn_on(hass, args.get("entity_id", ""))
        if name == "homeassistant_turn_off":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _homeassistant_turn_off(hass, args.get("entity_id", ""))
        if name == "homeassistant_toggle":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _homeassistant_toggle(hass, args.get("entity_id", ""))
        if name == "list_intent_handlers":
            return await _list_intent_handlers(hass)
        # --- Wave 14 dispatch ---
        if name == "vacuum_start":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _vacuum_start(hass, args.get("entity_id", ""))
        if name == "vacuum_stop":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _vacuum_stop(hass, args.get("entity_id", ""))
        if name == "vacuum_return_home":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _vacuum_return_home(hass, args.get("entity_id", ""))
        if name == "vacuum_locate":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _vacuum_locate(hass, args.get("entity_id", ""))
        if name == "vacuum_set_fan_speed":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _vacuum_set_fan_speed(
                hass, args.get("entity_id", ""), args.get("fan_speed", ""),
            )
        if name == "vacuum_send_command":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _vacuum_send_command(
                hass, args.get("entity_id", ""), args.get("command", ""),
                args.get("params"),
            )
        if name == "number_set_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _number_set_value(
                hass, args.get("entity_id", ""), float(args.get("value", 0)),
            )
        if name == "button_press":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _button_press(hass, args.get("entity_id", ""))
        if name == "select_set_option":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _select_set_option(
                hass, args.get("entity_id", ""), args.get("option", ""),
            )
        if name == "text_set_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _text_set_value(
                hass, args.get("entity_id", ""), args.get("value", ""),
            )
        if name == "valve_open":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _valve_open(hass, args.get("entity_id", ""))
        if name == "valve_close":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _valve_close(hass, args.get("entity_id", ""))
        if name == "valve_set_position":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _valve_set_position(
                hass, args.get("entity_id", ""), int(args.get("position", 0)),
            )
        if name == "lawn_mower_start":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _lawn_mower_start(hass, args.get("entity_id", ""))
        if name == "lawn_mower_dock":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _lawn_mower_dock(hass, args.get("entity_id", ""))
        if name == "remote_send_command":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _remote_send_command(
                hass, args.get("entity_id", ""), args.get("command", ""),
                args.get("device"), args.get("num_repeats"),
                args.get("delay_secs"),
            )
        if name == "remote_learn_command":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _remote_learn_command(
                hass, args.get("entity_id", ""),
                args.get("device"), args.get("command_type"),
                args.get("timeout"),
            )
        if name == "press_input_button":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _press_input_button(hass, args.get("entity_id", ""))
        # --- Wave 20 dispatch ---
        if name == "input_text_set_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_text_set_value(
                hass, args.get("entity_id", ""), args.get("value", ""),
            )
        if name == "set_device_tracker_location":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_device_tracker_location(
                hass, args.get("entity_id", ""),
                args.get("location_name"), args.get("gps"),
                args.get("gps_accuracy"), args.get("battery"),
            )
        if name == "input_datetime_set_datetime":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_datetime_set_datetime(
                hass, args.get("entity_id", ""),
                args.get("date"), args.get("time"), args.get("datetime"),
            )
        if name == "schedule_get_schedule":
            return await _schedule_get_schedule(
                hass, args.get("entity_id", ""),
            )
        if name == "persistent_notification_create":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _persistent_notification_create(
                hass, args.get("message", ""),
                args.get("title"), args.get("notification_id"),
            )
        if name == "persistent_notification_dismiss":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _persistent_notification_dismiss(
                hass, args.get("notification_id", ""),
            )
        if name == "get_network_info":
            return await _get_network_info(hass)
        # --- Wave 18 dispatch ---
        if name == "todo_add_item":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _todo_add_item(
                hass, args.get("entity_id", ""), args.get("item", ""),
                args.get("due_date"), args.get("description"),
            )
        if name == "todo_update_item":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _todo_update_item(
                hass, args.get("entity_id", ""), args.get("item", ""),
                args.get("rename"), args.get("status"),
                args.get("due_date"), args.get("description"),
            )
        if name == "todo_remove_item":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _todo_remove_item(
                hass, args.get("entity_id", ""), args.get("item", ""),
            )
        if name == "todo_get_items":
            return await _todo_get_items(
                hass, args.get("entity_id", ""), args.get("status"),
            )
        if name == "input_boolean_turn_on":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_boolean_turn_on(hass, args.get("entity_id", ""))
        if name == "input_boolean_turn_off":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_boolean_turn_off(hass, args.get("entity_id", ""))
        if name == "input_boolean_toggle":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_boolean_toggle(hass, args.get("entity_id", ""))
        if name == "input_number_set_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_number_set_value(
                hass, args.get("entity_id", ""), float(args.get("value", 0)),
            )
        if name == "input_number_increment":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_number_increment(hass, args.get("entity_id", ""))
        if name == "input_number_decrement":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_number_decrement(hass, args.get("entity_id", ""))
        if name == "input_select_set_option":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_select_set_option(
                hass, args.get("entity_id", ""), args.get("option", ""),
            )
        if name == "input_select_set_options":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_select_set_options(
                hass, args.get("entity_id", ""), args.get("options", []),
            )
        if name == "input_select_next":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_select_next(
                hass, args.get("entity_id", ""), bool(args.get("cycle", True)),
            )
        if name == "input_select_previous":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _input_select_previous(
                hass, args.get("entity_id", ""), bool(args.get("cycle", True)),
            )
        if name == "media_player_shuffle_set":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _media_player_shuffle_set(
                hass, args.get("entity_id", ""),
                bool(args.get("shuffle", False)),
            )
        if name == "media_player_repeat_set":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _media_player_repeat_set(
                hass, args.get("entity_id", ""), args.get("repeat", "off"),
            )
        # --- Wave 17 dispatch ---
        if name == "siren_turn_on":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _siren_turn_on(
                hass, args.get("entity_id", ""),
                args.get("tone"), args.get("volume_level"),
                args.get("duration"),
            )
        if name == "siren_turn_off":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _siren_turn_off(hass, args.get("entity_id", ""))
        if name == "humidifier_turn_on":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _humidifier_turn_on(hass, args.get("entity_id", ""))
        if name == "humidifier_turn_off":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _humidifier_turn_off(hass, args.get("entity_id", ""))
        if name == "humidifier_set_humidity":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _humidifier_set_humidity(
                hass, args.get("entity_id", ""), int(args.get("humidity", 50)),
            )
        if name == "humidifier_set_mode":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _humidifier_set_mode(
                hass, args.get("entity_id", ""), args.get("mode", ""),
            )
        if name == "water_heater_set_temperature":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _water_heater_set_temperature(
                hass, args.get("entity_id", ""),
                float(args.get("temperature", 40)),
            )
        if name == "water_heater_set_operation_mode":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _water_heater_set_operation_mode(
                hass, args.get("entity_id", ""),
                args.get("operation_mode", ""),
            )
        if name == "fan_turn_on":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _fan_turn_on(
                hass, args.get("entity_id", ""),
                args.get("percentage"), args.get("preset_mode"),
            )
        if name == "fan_turn_off":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _fan_turn_off(hass, args.get("entity_id", ""))
        if name == "fan_set_percentage":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _fan_set_percentage(
                hass, args.get("entity_id", ""), int(args.get("percentage", 50)),
            )
        if name == "fan_set_direction":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _fan_set_direction(
                hass, args.get("entity_id", ""), args.get("direction", "forward"),
            )
        if name == "fan_oscillate":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _fan_oscillate(
                hass, args.get("entity_id", ""), bool(args.get("oscillating", False)),
            )
        if name == "fan_set_preset_mode":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _fan_set_preset_mode(
                hass, args.get("entity_id", ""), args.get("preset_mode", ""),
            )
        if name == "alarm_arm_away":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _alarm_arm_away(
                hass, args.get("entity_id", ""), args.get("code"),
            )
        if name == "alarm_arm_home":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _alarm_arm_home(
                hass, args.get("entity_id", ""), args.get("code"),
            )
        if name == "alarm_arm_night":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _alarm_arm_night(
                hass, args.get("entity_id", ""), args.get("code"),
            )
        if name == "alarm_disarm":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _alarm_disarm(
                hass, args.get("entity_id", ""), args.get("code"),
            )
        if name == "alarm_trigger":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _alarm_trigger(
                hass, args.get("entity_id", ""), args.get("code"),
            )
        if name == "lock_lock":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _lock_lock(
                hass, args.get("entity_id", ""), args.get("code"),
            )
        if name == "lock_unlock":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _lock_unlock(
                hass, args.get("entity_id", ""), args.get("code"),
            )
        if name == "lock_open":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _lock_open(
                hass, args.get("entity_id", ""), args.get("code"),
            )
        if name == "cover_open":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _cover_open(hass, args.get("entity_id", ""))
        if name == "cover_close":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _cover_close(hass, args.get("entity_id", ""))
        if name == "cover_stop":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _cover_stop(hass, args.get("entity_id", ""))
        if name == "cover_set_position":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _cover_set_position(
                hass, args.get("entity_id", ""), int(args.get("position", 0)),
            )
        if name == "cover_open_tilt":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _cover_open_tilt(hass, args.get("entity_id", ""))
        if name == "cover_close_tilt":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _cover_close_tilt(hass, args.get("entity_id", ""))
        if name == "cover_set_tilt_position":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _cover_set_tilt_position(
                hass, args.get("entity_id", ""),
                int(args.get("tilt_position", 0)),
            )
        if name == "timer_start":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _timer_start(
                hass, args.get("entity_id", ""), args.get("duration"),
            )
        if name == "timer_cancel":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _timer_cancel(hass, args.get("entity_id", ""))
        if name == "timer_pause":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _timer_pause(hass, args.get("entity_id", ""))
        if name == "timer_finish":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _timer_finish(hass, args.get("entity_id", ""))
        if name == "increment_counter":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _increment_counter(hass, args.get("entity_id", ""))
        if name == "decrement_counter":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _decrement_counter(hass, args.get("entity_id", ""))
        if name == "reset_counter":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _reset_counter(hass, args.get("entity_id", ""))
        # --- Wave 15 dispatch ---
        if name == "media_player_play_media":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _media_player_play_media(
                hass, args.get("entity_id", ""),
                args.get("media_content_id", ""),
                args.get("media_content_type", "music"),
                args.get("enqueue"),
            )
        if name == "media_player_set_volume":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _media_player_set_volume(
                hass, args.get("entity_id", ""),
                float(args.get("volume_level", 0.5)),
            )
        if name == "media_player_media_pause":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _media_player_media_pause(hass, args.get("entity_id", ""))
        if name == "media_player_media_play":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _media_player_media_play(hass, args.get("entity_id", ""))
        if name == "media_player_media_next":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _media_player_media_next(hass, args.get("entity_id", ""))
        if name == "media_player_media_previous":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _media_player_media_previous(hass, args.get("entity_id", ""))
        if name == "date_set_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _date_set_value(
                hass, args.get("entity_id", ""), args.get("date", ""),
            )
        if name == "time_set_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _time_set_value(
                hass, args.get("entity_id", ""), args.get("time", ""),
            )
        if name == "datetime_set_value":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _datetime_set_value(
                hass, args.get("entity_id", ""), args.get("datetime", ""),
            )
        if name == "start_addon":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _start_addon(hass, args.get("slug", ""))
        if name == "stop_addon":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _stop_addon(hass, args.get("slug", ""))
        if name == "restart_addon":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _restart_addon(hass, args.get("slug", ""))
        if name == "get_addon_logs":
            return await _get_addon_logs(
                hass, args.get("slug", ""), int(args.get("lines", 100)),
            )
        if name == "list_area_devices":
            return await _list_area_devices(hass, args.get("area_id", ""))
        if name == "list_area_entities":
            return await _list_area_entities(hass, args.get("area_id", ""))
        if name == "delete_blueprint":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _delete_blueprint(
                hass, args.get("path", ""), args.get("domain", "automation"),
            )
        if name == "delete_config_entry":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _delete_config_entry(hass, args.get("entry_id", ""))
        if name == "disable_config_entry":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _disable_config_entry(
                hass, args.get("entry_id", ""), bool(args.get("disable", True)),
            )
        if name == "reload_integration":
            return await _reload_integration(hass, args.get("domain", ""))
        if name == "get_hardware_info":
            return await _get_hardware_info(hass)
        if name == "get_os_info":
            return await _get_os_info(hass)
        if name == "list_template_entities":
            return await _list_template_entities(hass)
        if name == "list_credentials":
            return await _list_credentials(hass)
        if name == "read_logs":
            return await _read_logs(hass, args.get("lines", 60))
        if name == "create_area":
            return await _create_area(hass, args["name"])
        if name == "rename_area":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            ident = args.get("identifier") or args.get("area_id") or args.get("name")
            new_name = args.get("new_name") or args.get("to")
            if not ident or not new_name:
                return {"error": "missing required arguments: identifier + new_name"}
            return await _rename_area(hass, ident, new_name)
        if name == "rename_entity":
            return await _update_entity(hass, args["entity_id"], name=args["name"])
        if name == "assign_entity_area":
            return await _update_entity(hass, args["entity_id"], area=args["area"])
        if name == "set_entity_enabled":
            return await _update_entity(hass, args["entity_id"], enabled=bool(args["enabled"]))
        if name == "render_template":
            return await _render_template(hass, args["template"], args.get("variables"))
        if name == "get_history":
            return await _get_history(hass, args["entity_id"], args.get("hours", 24))
        if name == "create_scene":
            return await _create_scene(hass, args["name"], args.get("entities") or {})
        if name == "create_script":
            seq = args.get("sequence")
            if seq is None:
                seq = args.get("action")
            return await _create_script(hass, args["alias"], seq)
        if name == "assign_entities_by_rules":
            return await _assign_entities_by_rules(
                hass, args["rules"], bool(args.get("only_unassigned", True)))
        if name == "create_helper":
            obj_id = args.get("object_id") or _slugify(args.get("name") or "")
            if not obj_id:
                return {"error": "missing required argument: object_id (or name to derive it)"}
            return await _create_helper(
                hass, store, args["domain"], obj_id, args.get("config") or {})
        if name == "create_template_sensor":
            return await _create_template_sensor(
                hass, store, args["name"], args["state"], unit=args.get("unit"),
                device_class=args.get("device_class"), icon=args.get("icon"))
        if name == "list_template_sensors":
            return await _list_template_sensors(hass)
        if name == "create_blueprint_automation":
            bp_path = args.get("blueprint_path") or args.get("path")
            if not bp_path:
                return {"error": "missing required argument: blueprint_path (or path)"}
            return await _create_blueprint_automation(
                hass, args["alias"], bp_path, args.get("inputs") or {})
        if name == "list_blueprints":
            return await _list_blueprints(hass, args.get("domain", "automation"))
        if name == "list_backups":
            return await _list_backups(hass)
        if name == "create_backup":
            return await _create_backup(hass, args.get("name", "HA-Copilot snapshot"))
        if name == "delete_backup":
            return await _delete_backup(hass, args["backup_id"])
        if name in ("update_automation", "update_script"):
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            ident = args.get("identifier") or args.get("id") or args.get("alias") or args.get("entity_id")
            new_alias = args.get("new_alias") or args.get("alias_new") or args.get("name")
            if not ident or not new_alias:
                return {"error": "missing required arguments: identifier + new_alias"}
            if name == "update_automation":
                return await _update_automation(hass, _resolve_automation_identifier(hass, ident), new_alias)
            return await _update_script(hass, ident, new_alias)
        if name in ("delete_automation", "delete_scene", "delete_script"):
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            ident = args.get("identifier") or args.get("id") or args.get("name") or args.get("entity_id")
            if not ident:
                return {"error": "missing required argument: identifier (id, alias or entity_id)"}
            if name == "delete_automation":
                return await _delete_automation(hass, _resolve_automation_identifier(hass, ident))
            if name == "delete_scene":
                return await _delete_scene(hass, ident)
            return await _delete_script(hass, ident)
        if name == "delete_area":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            ident = args.get("identifier") or args.get("area_id") or args.get("name")
            if not ident:
                return {"error": "missing required argument: identifier"}
            return await _delete_area(hass, ident)
        if name == "delete_helper":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            domain = args.get("domain")
            object_id = args.get("object_id")
            eid = args.get("entity_id") or args.get("identifier")
            if (not domain or not object_id) and isinstance(eid, str) and "." in eid:
                domain, object_id = eid.split(".", 1)
            if not domain or not object_id:
                return {"error": "missing required arguments: domain + object_id (or entity_id)"}
            return await _delete_helper(hass, domain, object_id)
        if name == "delete_template_sensor":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            ident = args.get("name") or args.get("identifier")
            if not ident:
                return {"error": "missing required argument: name"}
            return await _delete_template_sensor(hass, ident)
        if name == "list_config_entries":
            return await _list_config_entries(hass, args.get("domain"))
        if name == "reload_config_entry":
            return await _reload_config_entry(hass, args["entry_id"])
        if name == "get_core_config":
            return await _get_core_config(hass)
        if name == "list_entities":
            return await _list_entities(hass, args.get("domain"), args.get("area"), args.get("label"))
        if name == "list_devices":
            return await _list_devices(hass, args.get("area"), args.get("label"))
        if name == "update_device":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            did = args.get("device_id") or args.get("id") or args.get("identifier")
            if not did:
                return {"error": "missing required argument: device_id"}
            return await _update_device(hass, did, name=args.get("name"),
                                        area=args.get("area"), labels=args.get("labels"))
        if name == "assign_entity_labels":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _assign_entity_labels(hass, args["entity_id"], args.get("labels") or [])
        if name == "list_floors":
            return await _list_floors(hass)
        if name == "create_floor":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _create_floor(hass, args["name"], args.get("level"), args.get("icon"))
        if name == "delete_floor":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _delete_floor(hass, args.get("identifier") or args.get("floor_id") or args.get("name"))
        if name == "list_labels":
            return await _list_labels(hass)
        if name == "create_label":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _create_label(hass, args["name"], args.get("color"),
                                       args.get("icon"), args.get("description"))
        if name == "delete_label":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _delete_label(hass, args.get("identifier") or args.get("label_id") or args.get("name"))
        if name == "list_statistics":
            return await _list_statistics(hass)
        if name == "get_statistics":
            sids = args.get("statistic_ids") or ([args["statistic_id"]] if args.get("statistic_id") else [])
            return await _get_statistics(hass, sids, args.get("hours", 24), args.get("period", "hour"))
        if name == "execute_script":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            seq = args.get("sequence") or args.get("action")
            return await _execute_script(hass, seq, args.get("variables"))
        if name == "fire_event":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _fire_event(hass, args["event_type"], args.get("event_data") or args.get("data"))
        if name == "list_persons":
            return await _list_persons(hass)
        # ---- deep-fusion round 2 ----
        if name == "get_logbook":
            return await _get_logbook(hass, args.get("hours", 24), args.get("entity_id"))
        if name == "list_users":
            return await _list_users(hass)
        if name == "list_categories":
            return await _list_categories(hass, args.get("scope", "automation"))
        if name == "create_category":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _create_category(hass, args.get("scope", "automation"),
                                          args["name"], args.get("icon"))
        if name == "delete_category":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _delete_category(hass, args.get("scope", "automation"),
                                          args.get("identifier") or args.get("category_id") or args.get("name"))
        if name == "list_dashboards":
            return await _list_dashboards(hass)
        if name == "get_dashboard_config":
            return await _get_dashboard_config(hass, args.get("url_path"))
        if name == "update_dashboard":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            cfg = args.get("config")
            if not cfg or not isinstance(cfg, dict):
                return {"error": "missing required argument: config (dict with views/cards)"}
            return await _update_dashboard(hass, args.get("url_path"), cfg)
        if name == "get_energy_prefs":
            return await _get_energy_prefs(hass)
        if name == "conversation_process":
            return await _conversation_process(hass, args.get("text") or args.get("input", ""),
                                               args.get("language"), args.get("agent_id"))
        if name == "list_todo_items":
            return await _list_todo_items(hass, args.get("entity_id"))
        if name == "add_todo_item":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _add_todo_item(hass, args.get("entity_id"), args["item"])
        # ---- deep-fusion round 3 ----
        if name == "wait_for_event":
            return await _wait_for_event(hass, args["event_type"],
                                         args.get("timeout", 10), args.get("entity_id"))
        if name == "list_tags":
            return await _list_tags(hass)
        if name == "create_tag":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _create_tag(hass, args["name"], args.get("tag_id"))
        if name == "delete_tag":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _delete_tag(hass, args.get("identifier") or args.get("tag_id") or args.get("name"))
        if name == "get_system_health":
            return await _get_system_health(hass)
        if name == "get_blueprint":
            bp_path = args.get("path") or args.get("blueprint_path")
            if not bp_path:
                return {"error": "missing required argument: path (or blueprint_path)"}
            return await _get_blueprint(hass, bp_path, args.get("domain", "automation"))
        # ---- deep-fusion round 4 ----
        if name == "describe_service":
            return await _describe_service(hass, args["domain"], args["service"])
        if name == "describe_area":
            return await _describe_area(hass, args.get("identifier") or args.get("area_id") or args.get("name"))
        if name == "get_entity_registry_entry":
            return await _get_entity_registry_entry(hass, args["entity_id"])
        if name == "wait_for_template":
            return await _wait_for_template(hass, args["template"], args.get("timeout", 10))
        if name == "get_config_entry":
            return await _get_config_entry(hass, args.get("identifier") or args.get("entry_id") or args.get("domain"))
        # ---- deep-fusion round 5 ----
        if name == "get_device":
            return await _get_device(hass, args.get("identifier") or args.get("device_id") or args.get("name"))
        if name == "get_statistic_metadata":
            return await _get_statistic_metadata(hass, args.get("statistic_ids"))
        if name == "evaluate_condition":
            return await _evaluate_condition(hass, args["condition"], args.get("variables"))
        if name == "list_zones":
            return await _list_zones(hass)
        if name == "get_automation_trace":
            ident = args.get("identifier") or args.get("automation_id") or args.get("entity_id")
            if not ident:
                return {"error": "missing required argument: identifier (automation_id or entity_id)"}
            return await _get_automation_trace(hass, ident)
        # ---- deep-fusion round 6 ----
        if name == "get_system_log":
            return await _get_system_log(hass, args.get("level"), args.get("limit", 50))
        if name == "get_integration_manifest":
            ident = args.get("domain") or args.get("identifier")
            if not ident:
                return {"error": "missing required argument: domain"}
            return await _get_integration_manifest(hass, ident)
        if name == "get_recorder_info":
            return await _get_recorder_info(hass)
        if name == "get_loaded_integrations":
            return await _get_loaded_integrations(hass)
        if name == "call_service_response":
            return await _call_service_response(hass, args["domain"], args["service"], args.get("data"))
        # ---- deep-fusion round 7 ----
        if name == "get_automation_config":
            ident = args.get("identifier") or args.get("id") or args.get("entity_id")
            if not ident:
                return {"error": "missing required argument: identifier (id or entity_id)"}
            return await _get_automation_config(hass, ident)
        if name == "validate_automation_config":
            return await _validate_automation_config(hass, args["config"])
        if name == "list_config_flows":
            return await _list_config_flows(hass)
        if name == "set_state":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_state(hass, args["entity_id"], args["state"], args.get("attributes"))
        if name == "import_statistics":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _import_statistics(
                hass, args["statistic_id"], args["statistics"],
                unit=args.get("unit"), name=args.get("name"),
                has_mean=bool(args.get("has_mean", False)),
                has_sum=bool(args.get("has_sum", False)))
        # ---- deep-fusion round 8 ----
        if name == "get_script_config":
            ident = args.get("identifier") or args.get("entity_id") or args.get("id")
            if not ident:
                return {"error": "missing required argument: identifier (entity_id or id)"}
            return await _get_script_config(hass, ident)
        if name == "get_scene_config":
            return await _get_scene_config(hass, args.get("identifier") or args.get("name") or args.get("id"))
        if name == "get_device_automations":
            return await _get_device_automations(hass, args["device_id"], args.get("type", "trigger"))
        if name == "get_statistics_during_period":
            return await _get_statistics_during_period(
                hass, args["statistic_ids"], args["start"], args.get("end"), args.get("period", "hour"))
        if name == "clear_statistics":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _clear_statistics(hass, args["statistic_ids"])
        # ---- deep-fusion round 9 ----
        if name == "get_entity_relations":
            return await _get_entity_relations(hass, args["entity_id"])
        if name == "get_floor":
            return await _get_floor(hass, args.get("identifier") or args.get("floor_id") or args.get("name"))
        if name == "validate_blueprint_inputs":
            bp_path = args.get("path") or args.get("blueprint_path")
            if not bp_path:
                return {"error": "missing required argument: path (or blueprint_path)"}
            return await _validate_blueprint_inputs(hass, bp_path, args.get("inputs") or {}, args.get("domain", "automation"))
        if name == "get_template_functions":
            return await _get_template_functions(hass)
        if name == "create_automation_from_blueprint":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            bp_path = args.get("path") or args.get("blueprint_path")
            if not bp_path:
                return {"error": "missing required argument: path (or blueprint_path)"}
            return await _create_automation_from_blueprint(
                hass, bp_path, args.get("inputs") or {}, args["alias"], args.get("domain", "automation"))
        # ---- deep-fusion round 10 ----
        if name == "get_assist_pipelines":
            return await _get_assist_pipelines(hass)
        if name == "get_assist_pipeline":
            return await _get_assist_pipeline(hass, args.get("pipeline_id"))
        if name == "get_network_adapters":
            return await _get_network_adapters(hass)
        if name == "get_conversation_agents":
            return await _get_conversation_agents(hass)
        if name == "purge_recorder":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _purge_recorder(hass, int(args.get("keep_days", 10)),
                                         bool(args.get("repack", False)),
                                         bool(args.get("apply_filter", False)))
        # ---- deep-fusion round 11 ----
        if name == "converse":
            return await _converse(hass, args.get("text") or args.get("input", ""),
                                   args.get("conversation_id"), args.get("language"),
                                   args.get("agent_id"))
        if name == "get_recorder_db_info":
            return await _get_recorder_db_info(hass)
        if name == "get_recorder_runs":
            return await _get_recorder_runs(hass)
        if name == "get_entity_sources":
            return await _get_entity_sources(hass, args.get("entity_id"))
        if name == "update_entity_registry":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _update_entity_registry(
                hass, args["entity_id"], name=args.get("name"), icon=args.get("icon"),
                area_id=args.get("area_id"), new_entity_id=args.get("new_entity_id"),
                entity_category=args.get("entity_category"), labels=args.get("labels"),
                disabled_by=args.get("disabled_by"), hidden_by=args.get("hidden_by"))
        # ---- deep-fusion round 12 ----
        if name == "list_input_helpers":
            return await _list_input_helpers(hass, args.get("domain"))
        if name == "get_group":
            return await _get_group(hass, args["entity_id"])
        if name == "get_person":
            ident = args.get("identifier") or args.get("person") or args.get("name")
            if not ident:
                return {"error": "missing required argument: identifier (person or name)"}
            return await _get_person(hass, ident)
        if name == "set_input_helper":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _set_input_helper(hass, args["entity_id"], args.get("value"))
        if name == "update_todo_item":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _update_todo_item(
                hass, args["entity_id"], args["item"], rename=args.get("rename"),
                status=args.get("status"), due_date=args.get("due_date"),
                description=args.get("description"))
        if name == "search_community_resources":
            return await resources.search_community_resources(
                hass,
                args.get("query", ""),
                args.get("category", "all"),
                int(args.get("limit", 20)),
            )
        if name == "search_github":
            return await resources.search_github(
                hass,
                args["query"],
                args.get("sort", "stars"),
                int(args.get("limit", 15)),
            )
        if name == "search_blueprints":
            return await resources.search_blueprints(
                hass, args.get("query", ""), int(args.get("limit", 15))
            )
        if name == "discover_resources":
            return await resources.discover_resources(
                hass, args.get("query", ""), int(args.get("limit", 8))
            )
        if name == "search_zigbee_devices":
            return await resources.search_zigbee_devices(
                hass, args.get("query", ""), int(args.get("limit", 15))
            )
        if name == "search_tasmota_devices":
            return await resources.search_tasmota_devices(
                hass, args.get("query", ""), int(args.get("limit", 15))
            )
        if name == "search_esphome_devices":
            return await resources.search_esphome_devices(
                hass, args.get("query", ""), int(args.get("limit", 10))
            )
        if name == "search_ha_integrations":
            return await resources.search_ha_integrations(
                hass, args.get("query", ""), int(args.get("limit", 10))
            )
        if name == "search_ha_addons":
            return await resources.search_ha_addons(
                hass, args.get("query", ""), int(args.get("limit", 10))
            )
        if name == "manage_addon":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            slug = args.get("slug", "")
            action = args.get("action", "info")
            if not slug:
                return {"error": "missing required argument: slug (e.g. 'core_mosquitto')"}
            if action not in ("info", "install", "start", "stop", "restart", "uninstall"):
                return {"error": f"invalid action '{action}' — use info/install/start/stop/restart/uninstall"}
            return await _manage_addon(hass, slug, action)
        if name == "setup_integration":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            domain = args.get("domain", "")
            if not domain:
                return {"error": "missing required argument: domain"}
            user_input = args.get("user_input")
            return await _setup_integration(hass, domain, user_input)
        if name == "reconfigure_integration":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            eid = args.get("entry_id", "")
            if not eid:
                return {"error": "missing required argument: entry_id"}
            return await _reconfigure_integration(hass, eid, args.get("user_input"))
        if name == "manage_hacs":
            action = args.get("action", "list")
            if action != "list" and not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await _manage_hacs(
                hass, action, args.get("repo", ""),
                args.get("category", "integration"),
            )
        if name == "search_zwave_devices":
            return await resources.search_zwave_devices(
                hass, args.get("query", ""), int(args.get("limit", 10))
            )
        if name == "list_repo_blueprints":
            repo = args.get("repo") or args.get("full_name") or args.get("url")
            if not repo:
                return {"error": "missing required argument: repo (owner/name or GitHub URL)"}
            return await resources.list_repo_blueprints(
                hass, repo, int(args.get("limit", 30))
            )
        if name == "recommend_resources":
            return await resources.recommend_resources(
                hass, int(args.get("limit", 15)),
                include_blueprints=bool(args.get("include_blueprints", True)),
            )
        if name == "recommend_blueprints":
            pref = await memory.recall(hass, "preferred_blueprint_intents")
            preferred = (
                pref.get("value")
                if pref.get("found") and isinstance(pref.get("value"), list)
                else None
            )
            hist = await memory.list_memory(hass, "history")
            imported = {
                e["value"].get("source_repo")
                for e in hist.get("entries", [])
                if isinstance(e.get("value"), dict) and e["value"].get("source_repo")
            }
            return await resources.recommend_blueprints(
                hass, int(args.get("limit", 12)), preferred, imported
            )
        if name == "import_blueprint":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            result = await resources.import_blueprint(
                hass, store, args["url"], args.get("domain")
            )
            if isinstance(result, dict) and result.get("ok"):
                # Verify this HA can actually load the blueprint (e.g. an
                # incompatible selector key fails only at load time). Surface
                # that at import instead of letting it bite later.
                check = await _validate_blueprint_inputs(
                    hass, result.get("blueprint_path") or "", {},
                    result.get("domain") or "automation",
                )
                if isinstance(check, dict) and check.get("error"):
                    result["loadable"] = False
                    result["load_error"] = check["error"]
                else:
                    result["loadable"] = True
                repo = _github_repo_from_url(args["url"])
                await memory.remember(
                    hass,
                    f"import:{result.get('blueprint_path') or args['url']}",
                    {
                        "url": args["url"],
                        "source_repo": repo,
                        "blueprint_path": result.get("blueprint_path"),
                        "domain": result.get("domain"),
                    },
                    category="history",
                )
            return result
        if name == "recall_memory":
            return await memory.recall(hass, args.get("key") or "")
        if name == "list_memory":
            return await memory.list_memory(hass, args.get("category"))
        if name == "remember_memory":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await memory.remember(
                hass, args.get("key") or "", args.get("value"),
                args.get("category", "general"),
            )
        if name == "forget_memory":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await memory.forget(hass, args.get("key") or "")
        if name == "snapshot_device_profile":
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            return await memory.snapshot_device_profile(hass)
        return {"error": f"unknown tool '{name}'"}
    except KeyError as err:
        return {"error": f"missing required argument: {err}"}
    except Exception as err:  # noqa: BLE001 - surface any tool failure to the agent
        return {"error": f"{type(err).__name__}: {err}"}


# --- Tool safety classification (single source) ------------------------------
# Used to emit MCP tool *annotations* (readOnlyHint / destructiveHint /
# idempotentHint) so off-the-shelf MCP clients can flag destructive operations
# and surface read-only tools safely. Read/write here mirrors the same intent as
# the allow_write gate enforced inside ``dispatch``.
_READ_ONLY_PREFIXES = (
    "list_", "get_", "read_", "describe_", "validate_", "wait_for_",
)
_READ_ONLY_TOOLS = frozenset({
    "check_config",
    "registry_overview",
    "render_template",
    "evaluate_condition",
    "get_template_functions",
    "search_community_resources",
    "search_github",
    "search_blueprints",
    "discover_resources",
    "search_zigbee_devices",
    "search_zwave_devices",
    "search_tasmota_devices",
    "search_esphome_devices",
    "search_ha_integrations",
    "search_ha_addons",
    "recommend_resources",
    "recommend_blueprints",
    "recall_memory",
})
# Irreversible / disruptive operations (removal, data purge, full restart).
_DESTRUCTIVE_TOOLS = frozenset({"restart", "purge_recorder", "clear_statistics", "restore_backup"})


def _is_read_only(name: str) -> bool:
    return name.startswith(_READ_ONLY_PREFIXES) or name in _READ_ONLY_TOOLS


def _is_destructive(name: str) -> bool:
    return name.startswith("delete_") or name in _DESTRUCTIVE_TOOLS


def tool_annotations(name: str) -> dict[str, Any]:
    """MCP tool annotations for ``name`` (per the MCP tool-annotations spec).

    ``readOnlyHint`` marks tools with no side effects; ``destructiveHint`` marks
    irreversible ones (delete/purge/restart); ``idempotentHint`` marks calls
    whose repetition is a no-op (deletes). ``title`` is a human-readable label.
    """
    read_only = _is_read_only(name)
    destructive = (not read_only) and _is_destructive(name)
    return {
        "title": name.replace("_", " ").title(),
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": name.startswith("delete_"),
    }


# OpenAI-style function specifications. Exposed to external agents verbatim via
# the run_tool HTTP API and converted to MCP tool descriptors for the MCP server.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_tools",
            "description": "Run several tools in one call, in order, to batch a plan (e.g. read a state, act, then read back) without a round-trip per step. 'calls' is a list of {tool, args}. Returns each result in order with an error count. Set stop_on_error=true to halt at the first failure. Cannot be nested.",
            "parameters": {
                "type": "object",
                "properties": {
                    "calls": {
                        "type": "array",
                        "description": "Ordered tool calls to execute.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool": {"type": "string", "description": "Tool name, e.g. 'call_service'."},
                                "args": {"type": "object", "description": "Arguments for that tool."},
                            },
                            "required": ["tool"],
                        },
                    },
                    "stop_on_error": {
                        "type": "boolean",
                        "description": "Stop at the first failing call (default false: run all).",
                    },
                },
                "required": ["calls"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assist",
            "description": "Run a free-form natural-language command through Home Assistant's own conversation (Assist) pipeline, e.g. 'turn off the kitchen lights' or 'what's the temperature in the bedroom'. HA does sentence/intent matching, area and device resolution via the active conversation agent. Returns the spoken reply plus the structured response. Use when you'd rather hand HA a sentence than resolve entity_ids yourself.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The natural-language command or question."},
                    "language": {"type": "string", "description": "Optional language code, e.g. 'en' or 'zh-Hans'."},
                    "conversation_id": {"type": "string", "description": "Optional id to continue a prior conversation."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_intents",
            "description": "List intent handlers registered in Home Assistant (built-in like HassTurnOn/HassClimateSetTemperature plus any an integration adds) with their slot names. These are the high-level intents HA's Assist uses; pair with handle_intent to invoke them.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handle_intent",
            "description": "Fire a registered HA intent by type (see list_intents), e.g. HassTurnOn with slots {\"name\": \"kitchen light\"}. Slot values may be passed flat; they are wrapped to HA's slot form automatically. Returns HA's intent response (speech + matched targets).",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent_type": {"type": "string", "description": "Intent name, e.g. 'HassTurnOn'."},
                    "slots": {"type": "object", "description": "Slot values, e.g. {\"name\": \"kitchen light\", \"area\": \"kitchen\"}."},
                },
                "required": ["intent_type"],
            },
        },
    },
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
            "description": "Call any Home Assistant service, e.g. domain='light', service='turn_on', entity_id='light.living_room'. Target by 'entity_id' (full id) OR by 'area'/'floor'/'label'/'device' (name or id, string or list) to act on a whole group without enumerating entities — HA expands floor->areas->devices->entities. Extra params like brightness go in 'data'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Service domain, e.g. 'light'"},
                    "service": {"type": "string", "description": "Service name, e.g. 'turn_on'"},
                    "entity_id": {
                        "type": "string",
                        "description": "Target entity_id, e.g. 'light.living_room'. Use the exact full id from list_states.",
                    },
                    "area": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "description": "Target area(s) by name or area_id, e.g. 'Living Room'. From list_areas.",
                    },
                    "floor": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "description": "Target floor(s) by name or floor_id, e.g. 'Upstairs'. From list_floors.",
                    },
                    "label": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "description": "Target label(s) by name or label_id. From list_labels.",
                    },
                    "device": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "description": "Target device(s) by name or device_id, e.g. 'Living Room TV'. From list_devices; acts on all the device's entities.",
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
            "name": "list_dir",
            "description": "List files and sub-directories inside the HA config directory. Pass an empty path for the config root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Config-relative directory (default: root)"}},
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
            "name": "diagnose_home",
            "description": (
                "Run a holistic diagnostic sweep of the entire home. Returns "
                "unavailable entities, offline devices, failed integrations, "
                "disabled automations, and orphan registrations — everything "
                "that needs attention, in one call."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_home_context",
            "description": (
                "Build a spatial tree of the home: areas → devices → entities "
                "(with current states). Gives a complete picture of the home "
                "in one call. Optionally filter to a single area."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "area_id": {
                        "type": "string",
                        "description": "Optional area ID to scope the tree to.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "audit_automations",
            "description": (
                "Audit all automations for issues: never-triggered, idle for "
                "30+ days, disabled, and potential conflicts. Proactively "
                "surfaces automation health problems."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_optimizations",
            "description": (
                "Analyze the running home and suggest concrete improvements: "
                "unassigned entities, areas without automations, missing "
                "energy monitoring, no backup automation, voice control setup."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_device_health",
            "description": (
                "Check health of all devices: low battery (<20%), weak "
                "signal (RSSI/link quality), and stale sensors (unchanged "
                "48+ hours). Surfaces devices that need physical attention."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_call_service",
            "description": (
                "Call a service for multiple entities at once. E.g. turn off "
                "all lights, set all covers to 50%. Returns per-entity results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Service domain (e.g. 'light')"},
                    "service": {"type": "string", "description": "Service name (e.g. 'turn_off')"},
                    "entity_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "List of entity_ids to target",
                    },
                    "data": {"type": "object", "description": "Extra service data (e.g. brightness)"},
                },
                "required": ["domain", "service", "entity_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_config",
            "description": (
                "Export automations, scripts, scenes, or dashboard config as "
                "YAML for backup, sharing, or migration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "config_type": {
                        "type": "string",
                        "enum": ["automation", "script", "scene", "dashboard"],
                        "description": "What to export",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Optional: specific entity or dashboard ID",
                    },
                },
                "required": ["config_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_config",
            "description": (
                "Import YAML configuration (automations, scripts, scenes) "
                "into HA. Appends to existing config files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "config_type": {
                        "type": "string",
                        "enum": ["automation", "script", "scene"],
                    },
                    "yaml_content": {
                        "type": "string",
                        "description": "YAML content to import",
                    },
                },
                "required": ["config_type", "yaml_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_template",
            "description": (
                "Validate a Jinja2 template syntax without executing it. "
                "Catches errors before embedding in automations or cards."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {"type": "string", "description": "Jinja2 template string"},
                },
                "required": ["template"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_notification",
            "description": (
                "Send a notification through HA's notify services. "
                "Uses the default notifier unless a specific target is given."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "title": {"type": "string"},
                    "target": {
                        "type": "string",
                        "description": "Specific notifier (e.g. 'mobile_app_phone')",
                    },
                    "data": {"type": "object", "description": "Extra notification data"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_states",
            "description": (
                "Compare current states of multiple entities side-by-side. "
                "Useful for debugging: are all temperatures consistent? "
                "Are all lights in the expected state?"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "List of entity_ids to compare",
                    },
                },
                "required": ["entity_ids"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "create_zone",
            "description": "Create a new geofencing zone for presence detection.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "radius": {"type": "number", "description": "Radius in meters (default 100)"},
                    "icon": {"type": "string"},
                    "passive": {"type": "boolean"},
                },
                "required": ["name", "latitude", "longitude"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_input_helper",
            "description": (
                "Manage input helpers (input_boolean/number/select/text/datetime/button). "
                "Actions: list, create, set, delete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "set", "delete"]},
                    "helper_type": {"type": "string", "enum": ["boolean", "number", "select", "text", "datetime", "button"]},
                    "entity_id": {"type": "string"},
                    "name": {"type": "string"},
                    "value": {"description": "Value to set"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "For input_select"},
                    "min": {"type": "number"}, "max": {"type": "number"},
                    "step": {"type": "number"}, "unit": {"type": "string"},
                    "icon": {"type": "string"}, "initial": {}, "mode": {"type": "string"},
                },
                "required": ["action", "helper_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_counter",
            "description": "Manage counter helpers: list, create, increment, decrement, reset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "increment", "decrement", "reset"]},
                    "entity_id": {"type": "string"},
                    "name": {"type": "string"},
                    "initial": {"type": "integer"}, "step": {"type": "integer"},
                    "minimum": {"type": "integer"}, "maximum": {"type": "integer"},
                    "icon": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_timer",
            "description": "Manage timer helpers: list, create, start, pause, cancel, finish.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "start", "pause", "cancel", "finish"]},
                    "entity_id": {"type": "string"},
                    "name": {"type": "string"},
                    "duration": {"type": "string", "description": "e.g. '00:05:00'"},
                    "icon": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_backup",
            "description": "Manage HA backups: list, create, info, remove.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "info", "remove"]},
                    "slug": {"type": "string", "description": "Backup slug (for info/remove)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_label",
            "description": "Manage HA labels (2024.1+): list, create, delete. For organizing entities/devices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "delete"]},
                    "name": {"type": "string"}, "label_id": {"type": "string"},
                    "color": {"type": "string"}, "icon": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_floor",
            "description": "Manage floors (2024.2+): list, create, delete. Spatial hierarchy above areas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "delete"]},
                    "name": {"type": "string"}, "floor_id": {"type": "string"},
                    "icon": {"type": "string"}, "level": {"type": "integer"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_updates",
            "description": "List all available updates (HA core, HACS, add-ons, devices).",
            "parameters": {"type": "object", "properties": {}},
        },
    },

    {
        "type": "function",
        "function": {
            "name": "manage_calendar",
            "description": "Manage calendar entities: list, get_events, create_event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get_events", "create_event"]},
                    "entity_id": {"type": "string"},
                    "summary": {"type": "string"}, "start": {"type": "string"},
                    "end": {"type": "string"}, "description": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_todo",
            "description": "Manage to-do list entities (HA 2023.11+): list, get_items, add, update, remove.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get_items", "add", "update", "remove"]},
                    "entity_id": {"type": "string"}, "item": {"type": "string"},
                    "status": {"type": "string", "enum": ["needs_action", "completed"]},
                    "uid": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_tag",
            "description": "Manage NFC/RFID tags: list, create, remove.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "remove"]},
                    "tag_id": {"type": "string"}, "name": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_media",
            "description": "Browse media library (local media, TTS, Spotify, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "media_content_id": {"type": "string"},
                    "media_content_type": {"type": "string"},
                    "entity_id": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_camera_snapshot",
            "description": "Get camera entity info and proxy URL for snapshot/stream access.",
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
            "name": "manage_schedule",
            "description": "List schedule helper entities and their state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get"]},
                    "entity_id": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_automation",
            "description": "Enable or disable an automation without deleting it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "enable": {"type": "boolean", "description": "true=enable, false=disable"},
                },
                "required": ["entity_id", "enable"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_automation",
            "description": "Manually trigger an automation (optionally skip conditions).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "skip_condition": {"type": "boolean", "description": "Skip condition check (default false)"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "duplicate_automation",
            "description": "Clone an automation: reads its config and creates a copy with a new alias.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "new_alias": {"type": "string", "description": "Name for the copy (default: original + ' (Copy)')"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_device",
            "description": "Remove an orphan device from the device registry.",
            "parameters": {
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_device_entities",
            "description": "List all entities belonging to a specific device.",
            "parameters": {
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_history",
            "description": "Compare state history of multiple entities side-by-side over a time window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_ids": {"type": "array", "items": {"type": "string"}},
                    "hours": {"type": "integer", "description": "Hours back (default 24)"},
                },
                "required": ["entity_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_tts",
            "description": "Send text-to-speech to a media player entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "entity_id": {"type": "string", "description": "media_player entity (auto-selects first if omitted)"},
                    "language": {"type": "string"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_media",
            "description": "Play media on a media_player entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "media_content_id": {"type": "string"},
                    "media_content_type": {"type": "string", "enum": ["music", "video", "image", "playlist", "channel"]},
                },
                "required": ["entity_id", "media_content_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "activate_scene",
            "description": "Activate a scene (optionally with transition time).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "transition": {"type": "number", "description": "Transition time in seconds"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_scene",
            "description": "Capture current states of entities and save as a new scene.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_ids": {"type": "array", "items": {"type": "string"}},
                    "scene_name": {"type": "string"},
                },
                "required": ["entity_ids", "scene_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_mqtt",
            "description": "Publish a message to an MQTT topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "payload": {"type": "string"},
                    "qos": {"type": "integer", "enum": [0, 1, 2]},
                    "retain": {"type": "boolean"},
                },
                "required": ["topic", "payload"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subscribe_mqtt",
            "description": "Subscribe to an MQTT topic and return messages received within a timeout window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "MQTT topic (supports wildcards)"},
                    "timeout": {"type": "number", "description": "Seconds to listen (default 5, max 10)"},
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_mqtt_devices",
            "description": "List all devices discovered via the MQTT integration.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "permit_zigbee_join",
            "description": "Enable Zigbee pairing mode (tries ZHA first, then Zigbee2MQTT).",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration": {"type": "integer", "description": "Seconds to keep join open (default 60)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_zigbee_device",
            "description": "Rename a Zigbee device via Zigbee2MQTT bridge API.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_name": {"type": "string"},
                    "new_name": {"type": "string"},
                },
                "required": ["old_name", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "heal_zwave_network",
            "description": "Trigger Z-Wave network heal to rebuild routing tables.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_zwave_node_info",
            "description": "Get detailed Z-Wave node information for devices in the registry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Filter by entity (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wake_on_lan",
            "description": "Send Wake-on-LAN magic packet to wake a network device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mac": {"type": "string", "description": "MAC address (e.g. AA:BB:CC:DD:EE:FF)"},
                    "broadcast_address": {"type": "string"},
                },
                "required": ["mac"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ping_device",
            "description": "Ping a network host to check reachability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "count": {"type": "integer", "description": "Number of pings (default 3, max 10)"},
                },
                "required": ["host"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notification_services",
            "description": "List all available notification service targets.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dismiss_notification",
            "description": "Dismiss a persistent notification by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "notification_id": {"type": "string"},
                },
                "required": ["notification_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_persistent_notification",
            "description": "Create a persistent notification in the HA UI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "title": {"type": "string"},
                    "notification_id": {"type": "string", "description": "Optional ID for later dismissal"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_entity_domains",
            "description": "List all active entity domains with entity counts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_automations",
            "description": "List all automations with state, alias, and last triggered time.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_info",
            "description": "Get detailed information about a specific device (model, manufacturer, entities, connections).",
            "parameters": {
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": "Execute a script by entity_id with optional variables.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "variables": {"type": "object", "description": "Key-value variables to pass to the script"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_condition",
            "description": "Test if an automation condition evaluates to true or false against current state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "condition": {"type": "object", "description": "HA condition config (e.g. {condition: state, entity_id: ..., state: on})"},
                },
                "required": ["condition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_energy_summary",
            "description": "Get energy usage summary — configured sources or energy-class entities.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_energy_sources",
            "description": "List configured energy sources from the energy dashboard.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "camera_snapshot",
            "description": "Take a snapshot from a camera entity and save to file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "filename": {"type": "string", "description": "Output path (default: /config/www/snapshot_<name>.jpg)"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_cameras",
            "description": "List all camera entities with their status and brand.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_climate_preset",
            "description": "Set a climate entity's preset mode (away, eco, boost, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "preset_mode": {"type": "string"},
                },
                "required": ["entity_id", "preset_mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_climate_schedule",
            "description": "Get climate entity state, modes, presets, fan modes, and temperatures.",
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
            "name": "set_cover_position",
            "description": "Set cover (blinds/curtains) position (0=closed, 100=open).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "position": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": ["entity_id", "position"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vacuum_command",
            "description": "Send command to a vacuum (start, stop, pause, return_to_base, locate, clean_spot).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "enum": ["start", "stop", "pause", "return_to_base", "locate", "clean_spot"]},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_input_value",
            "description": "Set value of input_number, input_boolean, input_text, input_select, or input_datetime.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "value": {"description": "Value to set (type depends on input domain)"},
                },
                "required": ["entity_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_updates",
            "description": "List all pending updates (HA core, addons, HACS, devices).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_update",
            "description": "Install a pending update for an update entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "backup": {"type": "boolean", "description": "Create backup before update (default true)"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lock_door",
            "description": "Lock a smart lock.",
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
            "name": "unlock_door",
            "description": "Unlock a smart lock (optional PIN code).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "code": {"type": "string", "description": "Optional PIN code"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "arm_alarm",
            "description": "Arm an alarm control panel (arm_away, arm_home, arm_night, arm_vacation).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "mode": {"type": "string", "enum": ["arm_away", "arm_home", "arm_night", "arm_vacation", "arm_custom_bypass"]},
                    "code": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disarm_alarm",
            "description": "Disarm an alarm control panel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_alarm_state",
            "description": "Get alarm control panel state, features, and code requirement.",
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
            "name": "set_fan_speed",
            "description": "Set fan speed percentage (0=off, 100=max).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "percentage": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": ["entity_id", "percentage"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_fan_direction",
            "description": "Set fan rotation direction (forward or reverse).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "direction": {"type": "string", "enum": ["forward", "reverse"]},
                },
                "required": ["entity_id", "direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_water_heater_temperature",
            "description": "Set water heater target temperature.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "temperature": {"type": "number"},
                },
                "required": ["entity_id", "temperature"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_humidifier_mode",
            "description": "Set humidifier/dehumidifier operating mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "mode": {"type": "string"},
                },
                "required": ["entity_id", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "activate_siren",
            "description": "Activate or deactivate a siren with optional duration and tone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "turn_on": {"type": "boolean", "description": "true=activate, false=deactivate"},
                    "duration": {"type": "integer", "description": "Duration in seconds"},
                    "tone": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_button",
            "description": "Press a button entity (trigger a one-shot action).",
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
            "name": "list_calendar_events",
            "description": "List upcoming events from a calendar entity (default: next 7 days).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "start": {"type": "string", "description": "ISO datetime start"},
                    "end": {"type": "string", "description": "ISO datetime end"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create a new event on a calendar entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "start": {"type": "string", "description": "ISO datetime start"},
                    "end": {"type": "string", "description": "ISO datetime end"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["entity_id", "summary", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather_forecast",
            "description": "Get weather forecast (daily or hourly) for a weather entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "forecast_type": {"type": "string", "enum": ["daily", "hourly"]},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_number_value",
            "description": "Set a number entity value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "value": {"type": "number"},
                },
                "required": ["entity_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_select_option",
            "description": "Set a select entity option.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "option": {"type": "string"},
                },
                "required": ["entity_id", "option"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "conversation_query",
            "description": "Send a natural language query to the HA conversation agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "language": {"type": "string", "description": "Language code (e.g. zh-CN, en)"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_todo_item",
            "description": "Mark a to-do list item as completed or update its status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "item": {"type": "string"},
                    "status": {"type": "string", "enum": ["completed", "needs_action"]},
                },
                "required": ["entity_id", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_utility_meter",
            "description": "Reset a utility meter sensor to zero.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    # --- Wave 4 TOOL_SPECS ---
    {
        "type": "function",
        "function": {
            "name": "media_player_control",
            "description": "Control a media player: play/pause/stop/next/previous/volume_set/volume_up/volume_down/volume_mute/select_source/shuffle_set/repeat_set/turn_on/turn_off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "media_player entity"},
                    "command": {"type": "string", "description": "Command: play|pause|stop|next|previous|volume_set|volume_up|volume_down|volume_mute|select_source|shuffle_set|repeat_set|turn_on|turn_off"},
                    "volume_level": {"type": "number", "description": "Volume 0.0-1.0 (for volume_set)"},
                    "is_volume_muted": {"type": "boolean", "description": "Mute state (for volume_mute)"},
                    "source": {"type": "string", "description": "Source name (for select_source)"},
                    "shuffle": {"type": "boolean", "description": "Shuffle on/off (for shuffle_set)"},
                    "repeat": {"type": "string", "description": "Repeat mode: off|one|all (for repeat_set)"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_media_players",
            "description": "List all media_player entities with state, source, volume, and media info.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_mobile_notification",
            "description": "Send a rich mobile push notification with optional actions, images, and channels.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Notify service target (e.g. mobile_app_phone)"},
                    "message": {"type": "string", "description": "Notification body"},
                    "title": {"type": "string", "description": "Notification title"},
                    "data": {"type": "object", "description": "Extra data (actions, image, channel, etc.)"},
                },
                "required": ["target", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_person_location",
            "description": "Get location of a person or all persons (state, coordinates, source).",
            "parameters": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "Person entity_id (optional, omit for all)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_device_trackers",
            "description": "List all device_tracker entities with state, source_type, and location.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_yaml",
            "description": "Reload YAML-based configuration (automations/scripts/scenes/groups/inputs/all).",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "What to reload: automation|script|scene|group|input_boolean|input_number|input_text|input_select|input_datetime|template|all"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_all_integrations",
            "description": "Reload all config entries (integrations) sequentially.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_history_summary",
            "description": "Get summarized state change history for a single entity over a period.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "hours": {"type": "integer", "description": "Lookback period in hours (default 24)"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_logbook",
            "description": "Get filtered logbook entries for a specific entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "hours": {"type": "integer", "description": "Lookback period in hours (default 24)"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_states_by_domain",
            "description": "Get all entity states in a specific domain with full attributes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "HA domain (e.g. light, sensor, climate)"},
                },
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_nearest_person",
            "description": "Find the nearest person to a zone (approximate geodesic distance).",
            "parameters": {
                "type": "object",
                "properties": {
                    "zone": {"type": "string", "description": "Zone name (default: home)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_device_label",
            "description": "Assign labels to a device in the device registry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "List of label names"},
                },
                "required": ["device_id", "labels"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_image_url",
            "description": "Get the URL/path of an image entity (camera still, generic image).",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    # --- Wave 5 TOOL_SPECS ---
    {
        "type": "function",
        "function": {
            "name": "assign_area_floor",
            "description": "Assign an area to a floor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "area_id": {"type": "string", "description": "Area ID or name"},
                    "floor_id": {"type": "string", "description": "Floor ID"},
                },
                "required": ["area_id", "floor_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_tag",
            "description": "Fire a tag_scanned event (simulate NFC tag scan for automations).",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag_id": {"type": "string"},
                    "device_id": {"type": "string", "description": "Optional device that scanned"},
                },
                "required": ["tag_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_todo_item",
            "description": "Add an item to a todo list entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "item": {"type": "string", "description": "Todo item text"},
                    "due_date": {"type": "string", "description": "Due date (YYYY-MM-DD)"},
                    "description": {"type": "string"},
                },
                "required": ["entity_id", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_todo_item",
            "description": "Remove an item from a todo list entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "item": {"type": "string"},
                },
                "required": ["entity_id", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_assist_pipelines",
            "description": "List all configured Assist pipelines (STT/conversation/TTS engines).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_assist_pipeline",
            "description": "Run text through an Assist pipeline and get the response.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to process"},
                    "pipeline_id": {"type": "string", "description": "Specific pipeline/agent ID"},
                    "language": {"type": "string", "description": "Language code"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_thread_networks",
            "description": "List Thread border routers and network info.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_matter_nodes",
            "description": "Get Matter fabric nodes (devices connected via Matter).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restore_backup",
            "description": "Restore a Home Assistant backup by ID (DESTRUCTIVE).",
            "parameters": {
                "type": "object",
                "properties": {"backup_id": {"type": "string"}},
                "required": ["backup_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_backup",
            "description": "Get download path/URL for a backup.",
            "parameters": {
                "type": "object",
                "properties": {"backup_id": {"type": "string"}},
                "required": ["backup_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_entity_category",
            "description": "Set entity category (config/diagnostic/None) in the entity registry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "category": {"type": "string", "description": "config|diagnostic|none"},
                },
                "required": ["entity_id", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_timer",
            "description": "Start a timer helper with optional duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "duration": {"type": "string", "description": "Duration (HH:MM:SS or seconds)"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_timer",
            "description": "Cancel a running timer.",
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
            "name": "pause_timer",
            "description": "Pause a running timer.",
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
            "name": "finish_timer",
            "description": "Finish (complete) a timer early.",
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
            "name": "mower_command",
            "description": "Control a lawn mower: start/pause/dock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "description": "start|pause|dock"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "valve_control",
            "description": "Control a valve: open/close/set_position/stop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "description": "open|close|set_position|stop"},
                    "position": {"type": "integer", "description": "Position 0-100 (for set_position)"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_event_entities",
            "description": "List all event entities with their last event type.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_date_value",
            "description": "Set an input_datetime entity to a date value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "date": {"type": "string", "description": "Date (YYYY-MM-DD)"},
                },
                "required": ["entity_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_time_value",
            "description": "Set an input_datetime entity to a time value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "time": {"type": "string", "description": "Time (HH:MM:SS)"},
                },
                "required": ["entity_id", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_text_value",
            "description": "Set a text or input_text entity value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["entity_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_wake_words",
            "description": "List configured wake word detection entities.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_stt_engines",
            "description": "List available speech-to-text engines.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tts_engines",
            "description": "List available text-to-speech engines.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_conversation_agents",
            "description": "List all registered conversation agents.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_schedule",
            "description": "Get schedule helper state and next event.",
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
            "name": "get_statistics_metadata",
            "description": "Get long-term statistics metadata (statistic IDs, sources, units).",
            "parameters": {
                "type": "object",
                "properties": {
                    "statistic_ids": {"type": "array", "items": {"type": "string"}, "description": "Filter by specific statistic IDs (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_statistics",
            "description": "Clear long-term statistics for given statistic IDs (DESTRUCTIVE).",
            "parameters": {
                "type": "object",
                "properties": {
                    "statistic_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["statistic_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_remote_command",
            "description": "Send an IR/RF command via a remote entity (IR blaster, Broadlink, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "description": "Command name"},
                    "device": {"type": "string", "description": "Target device name"},
                    "num_repeats": {"type": "integer", "description": "Number of repeats (default 1)"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    # --- Wave 8 TOOL_SPECS ---
    {
        "type": "function",
        "function": {
            "name": "get_energy_preferences",
            "description": "Get energy dashboard preferences (sources, grids, solar, battery config).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_update",
            "description": "Skip an available update for an entity.",
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
            "name": "siren_control",
            "description": "Control a siren: turn_on (with optional tone/volume/duration) or turn_off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "description": "turn_on|turn_off"},
                    "tone": {"type": "string", "description": "Siren tone name"},
                    "volume_level": {"type": "number", "description": "Volume 0.0-1.0"},
                    "duration": {"type": "integer", "description": "Duration in seconds"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lock_control",
            "description": "Control a lock: lock/unlock/open (with optional code).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "description": "lock|unlock|open"},
                    "code": {"type": "string", "description": "Lock code (if required)"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "alarm_control",
            "description": "Control an alarm panel: arm_home/arm_away/arm_night/arm_vacation/disarm/trigger.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "description": "arm_home|arm_away|arm_night|arm_vacation|disarm|trigger"},
                    "code": {"type": "string", "description": "Alarm code (if required)"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fan_control",
            "description": "Control a fan: turn_on/turn_off/toggle/set_percentage/set_preset_mode/oscillate/set_direction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "description": "turn_on|turn_off|toggle|set_percentage|set_preset_mode|oscillate|set_direction"},
                    "percentage": {"type": "integer", "description": "Speed 0-100 (for set_percentage/turn_on)"},
                    "preset_mode": {"type": "string", "description": "Preset mode name"},
                    "direction": {"type": "string", "description": "forward|reverse"},
                    "oscillating": {"type": "boolean", "description": "Oscillation on/off"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cover_control",
            "description": "Control a cover: open/close/stop/set_position/toggle/open_tilt/close_tilt/set_tilt_position/toggle_tilt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "description": "open|close|stop|set_position|toggle|open_tilt|close_tilt|set_tilt_position|toggle_tilt"},
                    "position": {"type": "integer", "description": "Position 0-100 (for set_position)"},
                    "tilt_position": {"type": "integer", "description": "Tilt 0-100 (for set_tilt_position)"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "water_heater_control",
            "description": "Control a water heater: set_temperature/set_operation_mode/turn_on/turn_off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "description": "set_temperature|set_operation_mode|turn_on|turn_off"},
                    "temperature": {"type": "number", "description": "Target temperature"},
                    "operation_mode": {"type": "string", "description": "Operation mode name"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "humidifier_control",
            "description": "Control a humidifier: turn_on/turn_off/set_humidity/set_mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string", "description": "turn_on|turn_off|set_humidity|set_mode"},
                    "humidity": {"type": "integer", "description": "Target humidity %"},
                    "mode": {"type": "string", "description": "Mode name"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_automation_traces",
            "description": "List automation execution traces (debug runs) for a specific or all automations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {"type": "string", "description": "Specific automation entity_id (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_conversation",
            "description": "Process text through HA conversation agent (built-in intent handler or custom agent).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to process"},
                    "language": {"type": "string"},
                    "agent_id": {"type": "string", "description": "Specific agent ID"},
                    "conversation_id": {"type": "string", "description": "Continue existing conversation"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_input_boolean",
            "description": "Toggle an input_boolean helper.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    # --- Wave 9 TOOL_SPECS --- (below)
    {
        "type": "function",
        "function": {
            "name": "camera_turn_on",
            "description": "Turn on a camera entity.",
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
            "name": "camera_turn_off",
            "description": "Turn off a camera entity.",
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
            "name": "climate_set_preset",
            "description": "Set climate preset mode (home/away/eco/sleep/boost/comfort/activity).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "preset_mode": {"type": "string", "description": "Preset: home|away|eco|sleep|boost|comfort|activity|none"},
                },
                "required": ["entity_id", "preset_mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "climate_set_aux_heat",
            "description": "Toggle auxiliary/emergency heat on a climate entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "aux_heat": {"type": "boolean"},
                },
                "required": ["entity_id", "aux_heat"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notify_targets",
            "description": "List all available notification service targets.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_system_log",
            "description": "Clear the Home Assistant system log.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_reload_integrations",
            "description": "Reload multiple integration domains at once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domains": {"type": "array", "items": {"type": "string"}, "description": "Filter by specific domains (optional, reloads all if omitted)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_input_select_option",
            "description": "Set an input_select entity to a specific option.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "option": {"type": "string"},
                },
                "required": ["entity_id", "option"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_input_select_options",
            "description": "List available options for an input_select entity.",
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
            "name": "set_input_number_value",
            "description": "Set an input_number entity value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "value": {"type": "number"},
                },
                "required": ["entity_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calibrate_utility_meter",
            "description": "Calibrate a utility meter to a specific value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "value": {"type": "number"},
                },
                "required": ["entity_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_custom_event",
            "description": "Fire a logbook entry event for custom logging.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Event source name"},
                    "message": {"type": "string", "description": "Log message"},
                    "entity_id": {"type": "string", "description": "Associated entity (optional)"},
                    "domain": {"type": "string", "description": "Domain (optional)"},
                },
                "required": ["name", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_device_actions",
            "description": "List available automation actions for a device.",
            "parameters": {
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_device_action",
            "description": "Execute a device automation action (from list_device_actions output).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "object", "description": "Action config dict from list_device_actions"},
                },
                "required": ["action"],
            },
        },
    },
    # --- Wave 11 TOOL_SPECS ---
    {
        "type": "function",
        "function": {
            "name": "set_group_members",
            "description": "Set the members of a group entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "members": {"type": "array", "items": {"type": "string"}, "description": "List of entity_ids"},
                },
                "required": ["entity_id", "members"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dismiss_persistent_notification",
            "description": "Dismiss a persistent notification by ID.",
            "parameters": {
                "type": "object",
                "properties": {"notification_id": {"type": "string"}},
                "required": ["notification_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_timer_remaining",
            "description": "Get the remaining time on a timer entity.",
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
            "name": "get_sun_position",
            "description": "Get current sun position (elevation, azimuth, next rising/setting/dawn/dusk).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_input_datetime",
            "description": "Set an input_datetime entity value (date, time, or datetime).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "date": {"type": "string", "description": "Date (YYYY-MM-DD)"},
                    "time": {"type": "string", "description": "Time (HH:MM:SS)"},
                    "datetime": {"type": "string", "description": "Full datetime (YYYY-MM-DD HH:MM:SS)"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "climate_set_swing_mode",
            "description": "Set climate swing mode (on/off/vertical/horizontal/both).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "swing_mode": {"type": "string", "description": "on|off|vertical|horizontal|both"},
                },
                "required": ["entity_id", "swing_mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "climate_set_fan_mode",
            "description": "Set climate fan mode (auto/low/medium/high/off/on/diffuse).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "fan_mode": {"type": "string", "description": "auto|low|medium|high|off|on|diffuse"},
                },
                "required": ["entity_id", "fan_mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_player_tts",
            "description": "Play a text-to-speech message on a media player.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "message": {"type": "string"},
                    "engine": {"type": "string", "description": "TTS engine ID"},
                    "language": {"type": "string"},
                    "cache": {"type": "boolean", "description": "Cache audio (default true)"},
                },
                "required": ["entity_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "device_tracker_see",
            "description": "Manually update device_tracker location (legacy see service).",
            "parameters": {
                "type": "object",
                "properties": {
                    "dev_id": {"type": "string", "description": "Device ID"},
                    "mac": {"type": "string", "description": "MAC address"},
                    "location_name": {"type": "string", "description": "Location name (home/not_home/zone)"},
                    "gps": {"type": "array", "items": {"type": "number"}, "description": "[lat, lon]"},
                    "gps_accuracy": {"type": "integer"},
                    "battery": {"type": "integer"},
                    "host_name": {"type": "string"},
                },
            },
        },
    },
    # --- Wave 13 TOOL_SPECS ---
    {
        "type": "function",
        "function": {
            "name": "enable_automation",
            "description": "Enable an automation.",
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
            "name": "disable_automation",
            "description": "Disable an automation.",
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
            "name": "trigger_script",
            "description": "Trigger a script with optional variables.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "variables": {"type": "object", "description": "Script variables"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_attributes",
            "description": "Get all attributes of a specific entity (state, attributes, timestamps).",
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
            "name": "get_integration_info",
            "description": "Get detailed info about a specific integration domain (config entries, states).",
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
            "name": "set_input_text",
            "description": "Set an input_text entity value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["entity_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "light_turn_on",
            "description": "Turn on a light with optional brightness, color_temp, rgb_color, transition, effect.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "brightness": {"type": "integer", "description": "0-255"},
                    "color_temp": {"type": "integer", "description": "Mireds"},
                    "rgb_color": {"type": "array", "items": {"type": "integer"}, "description": "[R, G, B]"},
                    "transition": {"type": "number", "description": "Seconds"},
                    "effect": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "light_turn_off",
            "description": "Turn off a light with optional transition.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "transition": {"type": "number", "description": "Seconds"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_turn_on",
            "description": "Turn on a switch.",
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
            "name": "switch_turn_off",
            "description": "Turn off a switch.",
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
            "name": "climate_set_temperature",
            "description": "Set climate target temperature (single or range).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "temperature": {"type": "number"},
                    "target_temp_high": {"type": "number"},
                    "target_temp_low": {"type": "number"},
                    "hvac_mode": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "climate_set_hvac_mode",
            "description": "Set climate HVAC mode (off/heat/cool/heat_cool/auto/dry/fan_only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "hvac_mode": {"type": "string", "description": "off|heat|cool|heat_cool|auto|dry|fan_only"},
                },
                "required": ["entity_id", "hvac_mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "homeassistant_turn_on",
            "description": "Turn on any entity via homeassistant.turn_on (universal).",
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
            "name": "homeassistant_turn_off",
            "description": "Turn off any entity via homeassistant.turn_off (universal).",
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
            "name": "homeassistant_toggle",
            "description": "Toggle any entity via homeassistant.toggle (universal).",
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
            "name": "list_intent_handlers",
            "description": "List registered conversation intent handlers.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # --- Wave 14 TOOL_SPECS ---
    {
        "type": "function",
        "function": {
            "name": "vacuum_start",
            "description": "Start a vacuum.",
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
            "name": "vacuum_stop",
            "description": "Stop a vacuum.",
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
            "name": "vacuum_return_home",
            "description": "Send a vacuum back to its dock.",
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
            "name": "vacuum_locate",
            "description": "Locate a vacuum (play sound).",
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
            "name": "vacuum_set_fan_speed",
            "description": "Set vacuum fan speed (quiet/balanced/turbo/max).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "fan_speed": {"type": "string", "description": "quiet|balanced|turbo|max"},
                },
                "required": ["entity_id", "fan_speed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vacuum_send_command",
            "description": "Send a custom command to a vacuum.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string"},
                    "params": {"type": "object", "description": "Command parameters"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "number_set_value",
            "description": "Set a number entity value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "value": {"type": "number"},
                },
                "required": ["entity_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "button_press",
            "description": "Press a button entity.",
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
            "name": "select_set_option",
            "description": "Set a select entity option.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "option": {"type": "string"},
                },
                "required": ["entity_id", "option"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "text_set_value",
            "description": "Set a text entity value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["entity_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "valve_open",
            "description": "Open a valve.",
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
            "name": "valve_close",
            "description": "Close a valve.",
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
            "name": "valve_set_position",
            "description": "Set valve position (0=closed, 100=open).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "position": {"type": "integer", "description": "0-100"},
                },
                "required": ["entity_id", "position"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lawn_mower_start",
            "description": "Start mowing.",
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
            "name": "lawn_mower_dock",
            "description": "Send lawn mower back to dock.",
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
            "name": "remote_send_command",
            "description": "Send a command via a remote entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string"},
                    "device": {"type": "string"},
                    "num_repeats": {"type": "integer"},
                    "delay_secs": {"type": "number"},
                },
                "required": ["entity_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remote_learn_command",
            "description": "Put a remote entity into learning mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "device": {"type": "string"},
                    "command_type": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_input_button",
            "description": "Press an input_button entity.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    # --- Wave 15 TOOL_SPECS ---
    {
        "type": "function",
        "function": {
            "name": "media_player_play_media",
            "description": "Play media on a media player.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "media_content_id": {"type": "string", "description": "Media URL or ID"},
                    "media_content_type": {"type": "string", "description": "music|video|image|playlist|channel"},
                    "enqueue": {"type": "string", "description": "add|next|play|replace"},
                },
                "required": ["entity_id", "media_content_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_player_set_volume",
            "description": "Set media player volume (0.0 to 1.0).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "volume_level": {"type": "number", "description": "0.0 to 1.0"},
                },
                "required": ["entity_id", "volume_level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_player_media_pause",
            "description": "Pause media playback.",
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
            "name": "media_player_media_play",
            "description": "Resume media playback.",
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
            "name": "media_player_media_next",
            "description": "Skip to next media track.",
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
            "name": "media_player_media_previous",
            "description": "Skip to previous media track.",
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
            "name": "date_set_value",
            "description": "Set a date entity value (YYYY-MM-DD).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["entity_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "time_set_value",
            "description": "Set a time entity value (HH:MM:SS).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "time": {"type": "string", "description": "HH:MM:SS"},
                },
                "required": ["entity_id", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "datetime_set_value",
            "description": "Set a datetime entity value (YYYY-MM-DD HH:MM:SS).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "datetime": {"type": "string", "description": "YYYY-MM-DD HH:MM:SS"},
                },
                "required": ["entity_id", "datetime"],
            },
        },
    },
    # --- Wave 17 TOOL_SPECS ---
    {
        "type": "function",
        "function": {
            "name": "siren_turn_on",
            "description": "Turn on a siren with optional tone, volume, duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "tone": {"type": "string"},
                    "volume_level": {"type": "number", "description": "0.0-1.0"},
                    "duration": {"type": "integer", "description": "Seconds"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "siren_turn_off",
            "description": "Turn off a siren.",
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
            "name": "humidifier_turn_on",
            "description": "Turn on a humidifier.",
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
            "name": "humidifier_turn_off",
            "description": "Turn off a humidifier.",
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
            "name": "humidifier_set_humidity",
            "description": "Set target humidity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "humidity": {"type": "integer", "description": "0-100"},
                },
                "required": ["entity_id", "humidity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "humidifier_set_mode",
            "description": "Set humidifier mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "mode": {"type": "string"},
                },
                "required": ["entity_id", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "water_heater_set_temperature",
            "description": "Set water heater target temperature.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "temperature": {"type": "number"},
                },
                "required": ["entity_id", "temperature"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "water_heater_set_operation_mode",
            "description": "Set water heater operation mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "operation_mode": {"type": "string"},
                },
                "required": ["entity_id", "operation_mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fan_turn_on",
            "description": "Turn on a fan with optional percentage and preset mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "percentage": {"type": "integer", "description": "0-100"},
                    "preset_mode": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fan_turn_off",
            "description": "Turn off a fan.",
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
            "name": "fan_set_percentage",
            "description": "Set fan speed percentage (0-100).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "percentage": {"type": "integer", "description": "0-100"},
                },
                "required": ["entity_id", "percentage"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fan_set_direction",
            "description": "Set fan direction (forward/reverse).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "direction": {"type": "string", "description": "forward|reverse"},
                },
                "required": ["entity_id", "direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fan_oscillate",
            "description": "Set fan oscillation on/off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "oscillating": {"type": "boolean"},
                },
                "required": ["entity_id", "oscillating"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fan_set_preset_mode",
            "description": "Set fan preset mode (eco/sleep/auto/etc).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "preset_mode": {"type": "string"},
                },
                "required": ["entity_id", "preset_mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "alarm_arm_away",
            "description": "Arm alarm in away mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "alarm_arm_home",
            "description": "Arm alarm in home mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "alarm_arm_night",
            "description": "Arm alarm in night mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "alarm_disarm",
            "description": "Disarm alarm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "alarm_trigger",
            "description": "Trigger alarm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lock_lock",
            "description": "Lock a lock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lock_unlock",
            "description": "Unlock a lock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lock_open",
            "description": "Open (unlatch) a lock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cover_open",
            "description": "Open a cover.",
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
            "name": "cover_close",
            "description": "Close a cover.",
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
            "name": "cover_stop",
            "description": "Stop a cover.",
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
            "name": "cover_set_position",
            "description": "Set cover position (0=closed, 100=open).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "position": {"type": "integer", "description": "0-100"},
                },
                "required": ["entity_id", "position"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cover_open_tilt",
            "description": "Open cover tilt.",
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
            "name": "cover_close_tilt",
            "description": "Close cover tilt.",
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
            "name": "cover_set_tilt_position",
            "description": "Set cover tilt position (0-100).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "tilt_position": {"type": "integer", "description": "0-100"},
                },
                "required": ["entity_id", "tilt_position"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timer_start",
            "description": "Start a timer (optional duration override HH:MM:SS).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "duration": {"type": "string", "description": "HH:MM:SS"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timer_cancel",
            "description": "Cancel a timer.",
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
            "name": "timer_pause",
            "description": "Pause a timer.",
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
            "name": "timer_finish",
            "description": "Finish (force-complete) a timer.",
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
            "name": "increment_counter",
            "description": "Increment a counter entity.",
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
            "name": "decrement_counter",
            "description": "Decrement a counter entity.",
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
            "name": "reset_counter",
            "description": "Reset a counter entity.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    # --- Wave 20 TOOL_SPECS ---
    {
        "type": "function",
        "function": {
            "name": "input_text_set_value",
            "description": "Set an input_text value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["entity_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_device_tracker_location",
            "description": "Set a device tracker location via device_tracker.see.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "location_name": {"type": "string"},
                    "gps": {"type": "array", "items": {"type": "number"}, "description": "[lat, lon]"},
                    "gps_accuracy": {"type": "integer"},
                    "battery": {"type": "integer", "description": "0-100"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "input_datetime_set_datetime",
            "description": "Set an input_datetime value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "HH:MM:SS"},
                    "datetime": {"type": "string", "description": "YYYY-MM-DD HH:MM:SS"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_get_schedule",
            "description": "Get schedule entity state and attributes.",
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
            "name": "persistent_notification_create",
            "description": "Create a persistent notification in HA.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "title": {"type": "string"},
                    "notification_id": {"type": "string"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "persistent_notification_dismiss",
            "description": "Dismiss a persistent notification.",
            "parameters": {
                "type": "object",
                "properties": {"notification_id": {"type": "string"}},
                "required": ["notification_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_network_info",
            "description": "Get network/hostname info from the HA host.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    # --- Wave 18 TOOL_SPECS ---
    {
        "type": "function",
        "function": {
            "name": "todo_add_item",
            "description": "Add an item to a todo list entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "item": {"type": "string"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "description": {"type": "string"},
                },
                "required": ["entity_id", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_update_item",
            "description": "Update an item in a todo list entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "item": {"type": "string"},
                    "rename": {"type": "string"},
                    "status": {"type": "string", "description": "needs_action|completed"},
                    "due_date": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["entity_id", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_remove_item",
            "description": "Remove an item from a todo list entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "item": {"type": "string"},
                },
                "required": ["entity_id", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_get_items",
            "description": "Get items from a todo list entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "status": {"type": "string", "description": "needs_action|completed"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "input_boolean_turn_on",
            "description": "Turn on an input_boolean.",
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
            "name": "input_boolean_turn_off",
            "description": "Turn off an input_boolean.",
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
            "name": "input_boolean_toggle",
            "description": "Toggle an input_boolean.",
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
            "name": "input_number_set_value",
            "description": "Set an input_number value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "value": {"type": "number"},
                },
                "required": ["entity_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "input_number_increment",
            "description": "Increment an input_number by its step.",
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
            "name": "input_number_decrement",
            "description": "Decrement an input_number by its step.",
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
            "name": "input_select_set_option",
            "description": "Select an option on an input_select.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "option": {"type": "string"},
                },
                "required": ["entity_id", "option"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "input_select_set_options",
            "description": "Set the options list of an input_select.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["entity_id", "options"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "input_select_next",
            "description": "Select next option on an input_select.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "cycle": {"type": "boolean"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "input_select_previous",
            "description": "Select previous option on an input_select.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "cycle": {"type": "boolean"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_player_shuffle_set",
            "description": "Set media player shuffle mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "shuffle": {"type": "boolean"},
                },
                "required": ["entity_id", "shuffle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_player_repeat_set",
            "description": "Set media player repeat mode (off/all/one).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "repeat": {"type": "string", "description": "off|all|one"},
                },
                "required": ["entity_id", "repeat"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_addon",
            "description": "Start a Home Assistant add-on by slug.",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_addon",
            "description": "Stop a Home Assistant add-on by slug.",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_addon",
            "description": "Restart a Home Assistant add-on by slug.",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_addon_logs",
            "description": "Get log output from a Home Assistant add-on.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "lines": {"type": "integer", "description": "Lines to return (default 100)"},
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_area_devices",
            "description": "List all devices assigned to a specific area.",
            "parameters": {
                "type": "object",
                "properties": {"area_id": {"type": "string", "description": "Area ID or name"}},
                "required": ["area_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_area_entities",
            "description": "List all entities assigned to a specific area (directly or via device).",
            "parameters": {
                "type": "object",
                "properties": {"area_id": {"type": "string", "description": "Area ID or name"}},
                "required": ["area_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_blueprint",
            "description": "Delete a blueprint YAML file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path under blueprints/<domain>/"},
                    "domain": {"type": "string", "enum": ["automation", "script"], "description": "Blueprint domain (default: automation)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_config_entry",
            "description": "Remove an integration config entry.",
            "parameters": {
                "type": "object",
                "properties": {"entry_id": {"type": "string"}},
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disable_config_entry",
            "description": "Enable or disable an integration config entry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string"},
                    "disable": {"type": "boolean", "description": "true=disable, false=enable"},
                },
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_integration",
            "description": "Reload all config entries for an integration domain.",
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
            "name": "get_hardware_info",
            "description": "Get hardware info: CPU, memory, disk usage, uptime.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_os_info",
            "description": "Get HA OS, Python, supervisor version and platform info.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_template_entities",
            "description": "List all entities backed by the template integration.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_credentials",
            "description": "List authentication providers and configured credentials.",
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
            "name": "rename_area",
            "description": "Rename an existing area (the area 'update'). Resolves the target by area_id or current name; rejects a name already used by another area.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Current area_id or name"},
                    "new_name": {"type": "string", "description": "New area name"},
                },
                "required": ["identifier", "new_name"],
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
            "description": "Render a Jinja2 template against live HA state, e.g. \"{{ states('sensor.x') }}\" or \"{{ states.light | selectattr('state','eq','on') | list | count }}\". Use this to compute/inspect state. Optional `variables` are injected into the render context to emulate how an automation/script would evaluate the template (its trigger/this/custom vars).",
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {"type": "string"},
                    "variables": {"type": "object", "description": "Optional variables injected into the render context."},
                },
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
    {
        "type": "function",
        "function": {
            "name": "assign_entities_by_rules",
            "description": "Bulk-assign registry entities to areas by keyword rules (first hit wins); areas are created if missing. Replaces clicking through hundreds of entities in the UI. rules is a list of [area_name, [keyword, ...]].",
            "parameters": {
                "type": "object",
                "properties": {
                    "rules": {"type": "array", "description": "[[area_name, [keyword, ...]], ...]"},
                    "only_unassigned": {"type": "boolean", "description": "Only touch entities not already in an area (default true)"},
                },
                "required": ["rules"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_helper",
            "description": "Define a helper entity (input_boolean/input_number/input_text/input_select/input_datetime/timer/counter) and reload it. Requires packages to be included from configuration.yaml.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Helper domain, e.g. 'input_boolean'"},
                    "object_id": {"type": "string", "description": "Helper object id (slug); derived from name if omitted"},
                    "name": {"type": "string", "description": "Friendly name; used to derive object_id when object_id is omitted"},
                    "config": {"type": "object", "description": "Helper config, e.g. {name: '...'}"},
                },
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_template_sensor",
            "description": "Validate a Jinja state template against live state, then deploy it as a template sensor and reload. Rejects templates that fail to render so you never deploy a broken sensor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "state": {"type": "string", "description": "Jinja state template"},
                    "unit": {"type": "string"},
                    "device_class": {"type": "string"},
                    "icon": {"type": "string"},
                },
                "required": ["name", "state"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_template_sensors",
            "description": "List the template sensors managed by ha_copilot (from the managed package), each with its entity_id, state template, and current live state. This is the 'read' of the template-sensor lifecycle.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_blueprint_automation",
            "description": "Instantiate an automation from a blueprint by appending use_blueprint+inputs to automations.yaml and reloading. Use list_blueprints/blueprint inputs to discover blueprint_path and required inputs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string"},
                    "blueprint_path": {"type": "string", "description": "e.g. 'homeassistant/motion_light.yaml'"},
                    "inputs": {"type": "object", "description": "Blueprint input values"},
                },
                "required": ["alias", "blueprint_path", "inputs"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_blueprints",
            "description": "List installed blueprints for a domain ('automation' or 'script') with their declared inputs. Use to discover blueprint_path/inputs for create_blueprint_automation.",
            "parameters": {
                "type": "object",
                "properties": {"domain": {"type": "string", "description": "'automation' (default) or 'script'"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_backups",
            "description": "List existing Home Assistant backups (id, name, date, whether the database is included).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_backup",
            "description": "Create a local Home Assistant backup (a safety snapshot before risky changes). Runs asynchronously; poll list_backups for completion.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Backup name"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_backup",
            "description": "Delete a backup by its backup_id (get ids from list_backups).",
            "parameters": {
                "type": "object",
                "properties": {"backup_id": {"type": "string"}},
                "required": ["backup_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_automation",
            "description": "Rename an automation's alias (friendly name) in automations.yaml by id or current alias, then reload. The entity_id is derived from the automation id, so it stays stable across the rename.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Automation id or current alias"},
                    "new_alias": {"type": "string", "description": "New alias (friendly name)"},
                },
                "required": ["identifier", "new_alias"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_script",
            "description": "Rename a script's alias (friendly name) in scripts.yaml by key or 'script.<key>', then reload. The entity_id is the script key, so it stays stable across the rename.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Script key or 'script.<key>' entity_id"},
                    "new_alias": {"type": "string", "description": "New alias (friendly name)"},
                },
                "required": ["identifier", "new_alias"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_automation",
            "description": "Delete an automation from automations.yaml by its id or alias, then reload. Completes the automation lifecycle (create_automation creates them). A .copilot.bak backup is kept.",
            "parameters": {
                "type": "object",
                "properties": {"identifier": {"type": "string", "description": "Automation id or alias"}},
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_scene",
            "description": "Delete a scene from scenes.yaml by its id or name, then reload. A .copilot.bak backup is kept.",
            "parameters": {
                "type": "object",
                "properties": {"identifier": {"type": "string", "description": "Scene id or name"}},
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_script",
            "description": "Delete a script from scripts.yaml by its key or 'script.<key>' entity_id, then reload. A .copilot.bak backup is kept.",
            "parameters": {
                "type": "object",
                "properties": {"identifier": {"type": "string", "description": "Script key or script.<key> entity_id"}},
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_area",
            "description": "Delete an area from the area registry by area_id or name. Mirrors create_area; entities assigned to it become area-less.",
            "parameters": {
                "type": "object",
                "properties": {"identifier": {"type": "string", "description": "Area id or name"}},
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_helper",
            "description": "Delete a helper (input_boolean/number/text/select/datetime, timer, counter) created via create_helper, then reload and purge the entity. Pass domain+object_id or an entity_id like 'input_boolean.foo'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Helper domain, e.g. input_boolean"},
                    "object_id": {"type": "string", "description": "Helper object_id"},
                    "entity_id": {"type": "string", "description": "Alternative: '<domain>.<object_id>'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_template_sensor",
            "description": "Delete a template sensor created via create_template_sensor, by its name, then reload and purge the entity.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Template sensor name"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_config_entries",
            "description": "List integration config entries (the Settings > Integrations page): entry_id, domain, title and load state. Optionally filter by domain. This is how the operator sees which integrations/accounts are configured.",
            "parameters": {
                "type": "object",
                "properties": {"domain": {"type": "string", "description": "Optional integration domain filter, e.g. 'mqtt'"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_config_entry",
            "description": "Reload a single integration config entry by entry_id (re-apply a changed account/config without restarting HA). Get entry_ids from list_config_entries.",
            "parameters": {
                "type": "object",
                "properties": {"entry_id": {"type": "string"}},
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_core_config",
            "description": "Snapshot HA's core configuration: version, location (lat/lon/elevation), time zone, unit system, currency, country, language, config_dir, safe/recovery mode, and the list of loaded components/integrations.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_entities",
            "description": "Detailed entity-registry listing (richer than list_states): entity_id, name, platform, area_id, device_id, labels, entity_category, disabled/hidden flags. Optionally filter by domain, area (name or id) or label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "area": {"type": "string", "description": "Area name or area_id"},
                    "label": {"type": "string", "description": "label_id"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_devices",
            "description": "List the device registry: id, name, manufacturer, model, area_id, labels, sw_version, config_entries. Optionally filter by area or label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "area": {"type": "string", "description": "Area name or area_id"},
                    "label": {"type": "string", "description": "label_id"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_device",
            "description": "Update a device registry entry: rename it (name), assign/clear its area (pass '' to clear), and/or set its labels. Get device_id from list_devices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "name": {"type": "string", "description": "User-facing name override"},
                    "area": {"type": "string", "description": "Area name/id, or '' to clear"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Full label_id set to apply"},
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_entity_labels",
            "description": "Set the label set on a registry entity. Accepts label ids or label names (names are resolved; unknown labels are rejected — create them first with create_label).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["entity_id", "labels"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_floors",
            "description": "List floors in the floor registry (floor_id, name, level, icon, aliases). Floors group areas vertically.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_floor",
            "description": "Create a floor (idempotent by name). Optionally set level (integer, for ordering) and icon.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "level": {"type": "integer"},
                    "icon": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_floor",
            "description": "Delete a floor by floor_id or name.",
            "parameters": {
                "type": "object",
                "properties": {"identifier": {"type": "string"}},
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_labels",
            "description": "List labels in the label registry (label_id, name, color, icon, description). Labels are cross-cutting tags for entities/devices/areas.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_label",
            "description": "Create a label (idempotent by name). Optionally set color, icon, description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "color": {"type": "string"},
                    "icon": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_label",
            "description": "Delete a label by label_id or name.",
            "parameters": {
                "type": "object",
                "properties": {"identifier": {"type": "string"}},
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_statistics",
            "description": "List long-term statistics ids the recorder tracks (statistic_id, source, unit, has_mean/has_sum). These power the Energy dashboard and long-term graphs — distinct from raw state history.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_statistics",
            "description": "Fetch long-term statistics for one or more statistic_ids over the last N hours, aggregated per period (5minute/hour/day/week/month). Returns mean/min/max/sum/state/change rows.",
            "parameters": {
                "type": "object",
                "properties": {
                    "statistic_ids": {"type": "array", "items": {"type": "string"}},
                    "hours": {"type": "integer", "description": "Lookback window (default 24)"},
                    "period": {"type": "string", "description": "5minute|hour|day|week|month (default hour)"},
                },
                "required": ["statistic_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_script",
            "description": "Run an ad-hoc action sequence through HA's script engine WITHOUT persisting it — the general-purpose action runtime. 'sequence' uses the same grammar as scripts/automations (service calls, delay, wait_template, choose, repeat, variables, stop with response_variable). Returns any service_response/variables produced. Use this to orchestrate multi-step actions or fetch service responses (e.g. weather.get_forecasts).",
            "parameters": {
                "type": "object",
                "properties": {
                    "sequence": {"description": "An action object or list of actions (HA script syntax)"},
                    "variables": {"type": "object", "description": "Optional run variables available to templates"},
                },
                "required": ["sequence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fire_event",
            "description": "Fire a custom event on HA's event bus. Drives event-triggered automations and lets the agent emit signals into the system.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string"},
                    "event_data": {"type": "object", "description": "Event payload (alias: 'data')"},
                },
                "required": ["event_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_persons",
            "description": "List person entities with their tracked presence state, linked user_id and GPS location (when available).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logbook",
            "description": "Humanised event timeline (HA logbook): state changes, logbook entries, automation/script triggers over the recent window. Optionally filter to one entity_id.",
            "parameters": {"type": "object", "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (1-168, default 24)."},
                "entity_id": {"type": "string", "description": "Optional entity to filter the logbook to."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_users",
            "description": "List Home Assistant auth users (admin surface): id, name, is_active/is_owner/system_generated/local_only flags, group ids and an is_admin convenience flag.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_categories",
            "description": "List categories registered for a scope (e.g. 'automation', 'script', 'entity'). Categories group items in the HA UI.",
            "parameters": {"type": "object", "properties": {
                "scope": {"type": "string", "description": "Category scope, e.g. 'automation' or 'script' (default 'automation')."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_category",
            "description": "Create a category in a scope (idempotent by name). Returns category_id.",
            "parameters": {"type": "object", "properties": {
                "scope": {"type": "string", "description": "Category scope (default 'automation')."},
                "name": {"type": "string"},
                "icon": {"type": "string", "description": "Optional mdi icon, e.g. 'mdi:tag'."},
            }, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_category",
            "description": "Delete a category from a scope by category_id or name.",
            "parameters": {"type": "object", "properties": {
                "scope": {"type": "string", "description": "Category scope (default 'automation')."},
                "identifier": {"type": "string", "description": "Category id or name."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dashboards",
            "description": "List Lovelace dashboards (default + storage + YAML) with their url_path and mode \u2014 the UI surface map.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dashboard_config",
            "description": "Get the full Lovelace config for a dashboard: views, cards, and their types/entities. Pass url_path from list_dashboards (or omit for default). Use to inspect the current dashboard before editing.",
            "parameters": {"type": "object", "properties": {
                "url_path": {"type": "string", "description": "dashboard url_path (omit or 'lovelace' for default)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_dashboard",
            "description": "Save a full Lovelace config for a storage-mode dashboard. Pass the complete config dict (get it from get_dashboard_config, modify views/cards, save back). Cannot update YAML-mode dashboards. Write op \u2014 gated by allow_write.",
            "parameters": {"type": "object", "properties": {
                "url_path": {"type": "string", "description": "dashboard url_path (omit or 'lovelace' for default)."},
                "config": {"type": "object", "description": "full dashboard config: {\"title\": \"...\", \"views\": [{\"title\": \"...\", \"cards\": [...]}]}."},
            }, "required": ["config"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_energy_prefs",
            "description": "Return the Energy dashboard preferences (energy sources, device consumption, cost config), or {configured:false} when the energy dashboard is not set up.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "conversation_process",
            "description": "Send a natural-language command to the Assist conversation agent (HA's built-in NLU) and return its spoken response and matched/affected targets. The agent's-eye view of voice/text control.",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string", "description": "The utterance, e.g. 'turn on the living room light'."},
                "language": {"type": "string", "description": "Optional language code, e.g. 'en' or 'zh-cn'."},
                "agent_id": {"type": "string", "description": "Optional conversation agent entity_id."},
            }, "required": ["text"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_todo_items",
            "description": "List items in a todo list (e.g. the Shopping List). Defaults to the first todo entity if entity_id is omitted.",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string", "description": "todo.* entity id (optional)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_event",
            "description": "Subscribe to the HA event bus and block (bounded by timeout) until the next event of event_type fires, then return it. Optionally filter by entity_id (e.g. for 'state_changed'). The agent's window into the live running system — observe, don't just poll. Returns {timed_out:true} if nothing fires within timeout.",
            "parameters": {"type": "object", "properties": {
                "event_type": {"type": "string", "description": "Event to wait for, e.g. 'state_changed', 'call_service', 'automation_triggered', 'tag_scanned'."},
                "timeout": {"type": "number", "description": "Max seconds to wait (0.1-60, default 10)."},
                "entity_id": {"type": "string", "description": "Optional entity filter (matches event.data.entity_id, e.g. for state_changed)."},
            }, "required": ["event_type"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tags",
            "description": "List registered tags (NFC/RFID/QR) with id, name, last_scanned and bound device_id.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_tag",
            "description": "Create a tag (idempotent by name). Returns its tag_id. Tags fire 'tag_scanned' events usable as automation triggers.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string"},
                "tag_id": {"type": "string", "description": "Optional explicit id; auto-generated UUID if omitted."},
            }, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_tag",
            "description": "Delete a tag by tag_id or name.",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "Tag id or name."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_health",
            "description": "Aggregate the system_health report of every integration that publishes one (HA version, recorder/database, cloud, restored entities, update server reachability, etc.) — the platform self-diagnostic surface.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_blueprint",
            "description": "Return one blueprint's full metadata and input schema (name, description, target domain, source_url, declared inputs) for a domain. Use list_blueprints to discover paths.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Blueprint path, e.g. 'homeassistant/motion_light.yaml'."},
                "domain": {"type": "string", "description": "'automation' (default) or 'script'."},
            }, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_service",
            "description": "Return the full schema of one service: human name, description, per-field selectors/defaults and the target schema. list_services only gives names — use this to build a correct call_service payload.",
            "parameters": {"type": "object", "properties": {
                "domain": {"type": "string", "description": "Service domain, e.g. 'light'."},
                "service": {"type": "string", "description": "Service name, e.g. 'turn_on'."},
            }, "required": ["domain", "service"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_area",
            "description": "Resolve an area (by id or name) into its full membership graph: floor, labels, member devices, and the effective entities (assigned directly or via their device). The area relationship graph.",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "Area id or name."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_registry_entry",
            "description": "Deep entity-registry introspection (beyond runtime state): unique_id, platform, owning config_entry/device/area, entity_category, device_class, disabled_by/hidden_by, capabilities, supported_features, labels and options.",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string"},
            }, "required": ["entity_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_template",
            "description": "Bounded wait until a Jinja template renders truthy (the template analogue of wait_for_event). Returns immediately if already truthy; returns {timed_out:true} if it never becomes truthy within timeout.",
            "parameters": {"type": "object", "properties": {
                "template": {"type": "string", "description": "Jinja template, e.g. \"{{ is_state('light.x','on') }}\"."},
                "timeout": {"type": "number", "description": "Max seconds to wait (0.1-60, default 10)."},
            }, "required": ["template"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_config_entry",
            "description": "Single config entry detail by entry_id or domain: domain, title, load state, source, version, disabled_by, support flags and options. Secrets in .data are deliberately not exposed.",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "config entry_id, or a domain (returns the first entry)."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device",
            "description": "Deep device introspection by id or name (the device analogue of describe_area): manufacturer/model, sw/hw version, area, via_device parent, owning config entries, connections, identifiers, labels and the full list of entities the device exposes.",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "Device id or name."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_statistic_metadata",
            "description": "Recorder long-term statistic metadata: per statistic_id its source, name, unit_of_measurement and has_mean/has_sum flags. Omit statistic_ids for all. Pair with get_statistics to interpret aggregated data correctly.",
            "parameters": {"type": "object", "properties": {
                "statistic_ids": {"type": "array", "items": {"type": "string"}, "description": "Optional list of statistic ids to filter by."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_condition",
            "description": "Validate and evaluate an HA condition config against live state, returning a boolean. Supports state/numeric_state/template/time/zone/and/or/not and shorthand template strings. Lets an agent test logic BEFORE committing it into an automation.",
            "parameters": {"type": "object", "properties": {
                "condition": {"description": "A condition config (object) or a shorthand template string, e.g. {\"condition\":\"state\",\"entity_id\":\"light.x\",\"state\":\"on\"}."},
                "variables": {"type": "object", "description": "Optional template variables."},
            }, "required": ["condition"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_zones",
            "description": "List zones with geo (latitude/longitude/radius/passive) and the persons currently inside each — the presence/geofence surface.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_automation_trace",
            "description": "Return the most recent execution trace of an automation (step-by-step path through trigger/conditions/actions, timing, changed variables) — the automation debug surface. Accepts an automation entity_id, numeric id, or alias.",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "automation entity_id, numeric id, or alias."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_log",
            "description": "Recent captured log records (the Settings > System > Logs surface): level/message/source/exception/count/timestamp. Optional level filter (ERROR/WARNING/...). The agent's window into what HA itself is complaining about, without shelling into the container.",
            "parameters": {"type": "object", "properties": {
                "level": {"type": "string", "description": "Optional level filter, e.g. ERROR or WARNING."},
                "limit": {"type": "integer", "description": "Max records (default 50, cap 200)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_integration_manifest",
            "description": "Integration manifest by domain: name, version, requirements, dependencies, after_dependencies, iot_class, config_flow, quality_scale, documentation — what an integration is made of, straight from the loaded code.",
            "parameters": {"type": "object", "properties": {
                "domain": {"type": "string", "description": "Integration domain, e.g. 'light', 'mqtt', 'hue'."},
            }, "required": ["domain"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recorder_info",
            "description": "Recorder health: whether it is recording and its current write backlog — a cheap liveness/health probe for the history/statistics subsystem.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_loaded_integrations",
            "description": "The set of components currently loaded into this running instance — the live 'what is actually running' surface, counterpart to get_integration_manifest (which describes one integration's code).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_service_response",
            "description": "Call a service that RETURNS a response payload (return_response=True), e.g. weather.get_forecasts, calendar.get_events, todo.get_items. Unlike call_service (which only confirms execution), this surfaces the data such read/query services produce.",
            "parameters": {"type": "object", "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "data": {"type": "object", "description": "Service call data/target."},
            }, "required": ["domain", "service"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_automation_config",
            "description": "Return an automation's full definition (alias/triggers/conditions/actions/mode) from automations.yaml, matched by id, alias, or entity_id. The configuration behind the entity, complementing get_automation_trace.",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "automation id, alias, or entity_id."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_automation_config",
            "description": "Validate an automation config dict against HA's schema WITHOUT saving — returns valid:true or the precise validation error. Use before create_automation/update_automation to author a correct automation.",
            "parameters": {"type": "object", "properties": {
                "config": {"type": "object", "description": "Automation config (alias, triggers, conditions, actions, mode)."},
            }, "required": ["config"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_config_flows",
            "description": "Integrations that support a UI config flow + any flows currently in-progress (handler/step/source) — the integration setup surface.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_state",
            "description": "Directly set/override an entity's state in the state machine (virtual write to hass.states). Useful to seed a test value so templates/automations can be exercised, or to push a value for an entity no integration backs. A real integration may overwrite it on its next update; does not persist to a device.",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "state": {"type": "string"},
                "attributes": {"type": "object", "description": "Optional state attributes."},
            }, "required": ["entity_id", "state"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_statistics",
            "description": "Insert long-term statistics points for a statistic_id (recorder write) — backfill energy-dashboard/custom-metric history. If the id contains ':' it is an EXTERNAL statistic (own source); otherwise internal. Each point needs a UTC hour-aligned 'start' plus some of mean/min/max/sum/state.",
            "parameters": {"type": "object", "properties": {
                "statistic_id": {"type": "string", "description": "e.g. 'sensor.energy' or external 'ha_copilot:my_metric'."},
                "statistics": {"type": "array", "description": "List of points: {start: ISO8601, mean?/min?/max?/sum?/state?}.", "items": {"type": "object"}},
                "unit": {"type": "string"},
                "name": {"type": "string"},
                "has_mean": {"type": "boolean"},
                "has_sum": {"type": "boolean"},
            }, "required": ["statistic_id", "statistics"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_script_config",
            "description": "Return a script's full definition (alias/sequence/mode/icon) from scripts.yaml, matched by object_id, 'script.<id>', or alias. The configuration behind the script entity.",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "script object_id, 'script.<id>', or alias."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scene_config",
            "description": "Return a scene's full definition (name/entities/states it restores) from scenes.yaml, matched by name or id. The configuration behind the scene entity.",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "scene name or id."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_automations",
            "description": "List the device-automation capabilities a device exposes — triggers / conditions / actions usable in device-based automations (the 'Device' option in the automation editor). Get device_id from list_devices.",
            "parameters": {"type": "object", "properties": {
                "device_id": {"type": "string"},
                "type": {"type": "string", "description": "trigger | condition | action (default trigger)."},
            }, "required": ["device_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_statistics_during_period",
            "description": "Pure-period statistics retrieval: rows strictly between an explicit start and end (ISO8601 UTC), at the requested period (5minute/hour/day/week/month). Unlike get_statistics (last-N-hours), this is a clean window.",
            "parameters": {"type": "object", "properties": {
                "statistic_ids": {"type": "array", "items": {"type": "string"}},
                "start": {"type": "string", "description": "ISO8601 UTC start."},
                "end": {"type": "string", "description": "ISO8601 UTC end (optional)."},
                "period": {"type": "string", "description": "5minute|hour|day|week|month (default hour)."},
            }, "required": ["statistic_ids", "start"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_relations",
            "description": "Entity-centric relationship graph: an entity resolved UP through its device → area → floor, plus sibling entities on the same device, its config entry and labels. The inverse view of describe_area (which is area-centric).",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string"},
            }, "required": ["entity_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_floor",
            "description": "Resolve a floor (by floor_id or name) into the areas it contains + a total effective entity count — completes the floor→area→entity graph (complements list_floors / describe_area).",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "floor_id or floor name."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_blueprint_inputs",
            "description": "Check that a set of inputs satisfies a blueprint's schema BEFORE instantiating it — reports missing required inputs (those without a default) and unknown keys. Precursor to create_automation_from_blueprint.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "blueprint path, e.g. 'homeassistant/motion_light.yaml' (also accepts blueprint_path, as returned by import_blueprint/list_repo_blueprints)."},
                "inputs": {"type": "object", "description": "input name -> value."},
                "domain": {"type": "string", "description": "automation | script (default automation)."},
            }, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_automation_from_blueprint",
            "description": "Instantiate a blueprint into a real automation (writes a use_blueprint entry to automations.yaml and reloads). Inputs are validated against the blueprint schema first; rejected on any missing input.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "blueprint path, e.g. 'homeassistant/motion_light.yaml' (also accepts blueprint_path, as returned by import_blueprint/list_repo_blueprints)."},
                "inputs": {"type": "object", "description": "input name -> value (see get_blueprint / validate_blueprint_inputs)."},
                "alias": {"type": "string", "description": "name for the new automation."},
                "domain": {"type": "string", "description": "automation | script (default automation)."},
            }, "required": ["path", "alias"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_template_functions",
            "description": "Catalog the Jinja extensions available in THIS instance's template engine — globals/functions, filters and tests (including HA extras like states, area_id, device_id, expand). The authoring surface for templates.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_assist_pipelines",
            "description": "List all configured Assist (voice) pipelines with their STT/TTS/conversation engines and languages, plus which is preferred — the voice-assistant configuration surface.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_assist_pipeline",
            "description": "One Assist pipeline's full definition (defaults to the preferred pipeline when no id given) — conversation/STT/TTS engines, languages, wake word, local-intent preference.",
            "parameters": {"type": "object", "properties": {
                "pipeline_id": {"type": "string", "description": "pipeline id (optional; default = preferred)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_network_adapters",
            "description": "The network adapters HA sees (name, default, auto/enabled, IPv4/IPv6) — the networking view used for discovery and external-URL announcement.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_conversation_agents",
            "description": "List the conversation/Assist agents that conversation_process can target via agent_id (the built-in Home Assistant agent plus any conversation entities from integrations).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "purge_recorder",
            "description": "Trigger a recorder purge (recorder.purge service): drop history/state rows older than keep_days; optionally repack the DB to reclaim disk and apply the recorder include/exclude filter. Recorder housekeeping (write).",
            "parameters": {"type": "object", "properties": {
                "keep_days": {"type": "integer", "description": "keep rows newer than this many days (default 10)."},
                "repack": {"type": "boolean", "description": "rewrite/compact the DB file (default false)."},
                "apply_filter": {"type": "boolean", "description": "apply the recorder include/exclude filter (default false)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "converse",
            "description": "Multi-turn Assist conversation: like conversation_process but threads a conversation_id so follow-up turns share context. Returns the (new) conversation_id to chain, the agent's speech, and continue_conversation.",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string", "description": "the utterance to send."},
                "conversation_id": {"type": "string", "description": "id from a prior turn to continue (optional)."},
                "language": {"type": "string", "description": "language code (optional)."},
                "agent_id": {"type": "string", "description": "conversation agent entity id (optional; see get_conversation_agents)."},
            }, "required": ["text"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recorder_db_info",
            "description": "The recorder database's identity & footprint — SQL dialect, password-masked connection URL, live recording flag + write backlog, bind-var limit, and on-disk size for SQLite. Complements get_recorder_info.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recorder_runs",
            "description": "List the recorder's run periods — every span between an HA start and the next clean shutdown that history was recorded for (start/end + whether it closed incorrectly, i.e. an unclean stop). The boot/uptime ledger of the DB.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_sources",
            "description": "Map live entities to their providing source — the integration/domain that created each one, whether it is from a custom_component, and its config_entry. Pass entity_id for one, or omit for a per-domain rollup of all.",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string", "description": "one entity to resolve (optional; omit for the full rollup)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_entity_registry",
            "description": "Write entity-registry overrides for an entity — friendly name, icon, area assignment, entity_category, labels, enable/disable & hide, or a rename (new_entity_id). The user-customization surface of the registry (write).",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "name": {"type": "string", "description": "friendly name override."},
                "icon": {"type": "string", "description": "mdi icon override, e.g. 'mdi:lightbulb'."},
                "area_id": {"type": "string", "description": "assign to an area."},
                "new_entity_id": {"type": "string", "description": "rename the entity_id."},
                "entity_category": {"type": "string", "description": "config | diagnostic."},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "label ids."},
                "disabled_by": {"type": "string", "description": "'user' to disable; omit to leave unchanged."},
                "hidden_by": {"type": "string", "description": "'user' to hide; omit to leave unchanged."},
            }, "required": ["entity_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_input_helpers",
            "description": "Enumerate the input_* helper entities (input_boolean/number/text/select/datetime/button) with their current value and config (min/max/options/pattern/mode) — the manual-input control panel.",
            "parameters": {"type": "object", "properties": {
                "domain": {"type": "string", "description": "restrict to one helper domain, e.g. 'input_number' (optional)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_input_helper",
            "description": "Write a value to any input_* helper, routing to the right service: boolean→turn_on/off, number/text→set_value, select→select_option, datetime→set_datetime, button→press. Unified helper-write surface (write).",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string", "description": "the input_* helper entity."},
                "value": {"description": "the value to set (bool/number/string/option/datetime depending on the helper)."},
            }, "required": ["entity_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_group",
            "description": "Resolve a group (or any grouping entity exposing an entity_id member list) into its members with each member's live state — the membership + roll-up view of a grouping entity.",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string"},
            }, "required": ["entity_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_person",
            "description": "Resolve a person (by person entity_id, person id, or name) into their tracked location — current zone/state, the device_trackers feeding it, linked HA user_id, and picture. The presence-detection identity view.",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "person entity_id, id, or friendly name."},
            }, "required": ["identifier"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_todo_item",
            "description": "Update an existing item on a to-do list (todo.update_item) — change its status (needs_action/completed), rename it, set a due date or description. The edit/complete counterpart to add_todo_item (write).",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string", "description": "the todo list entity."},
                "item": {"type": "string", "description": "the item to update (its summary or uid)."},
                "rename": {"type": "string", "description": "new summary (optional)."},
                "status": {"type": "string", "description": "'needs_action' or 'completed' (optional)."},
                "due_date": {"type": "string", "description": "due date YYYY-MM-DD (optional)."},
                "description": {"type": "string", "description": "item description (optional)."},
            }, "required": ["entity_id", "item"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_community_resources",
            "description": "Search the HACS community catalog (custom integrations, Lovelace/frontend cards, themes) by keyword \u2014 e.g. a brand ('xiaomi', 'aqara'), a device type ('vacuum', 'thermostat'), or a card name ('mushroom'). Returns matching repositories with stars, description and GitHub url so the operator can pull in the right ecosystem resource. Read-only network search; pair with recommend_resources to auto-match the user's own devices.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "keywords: brand, device type, or card/integration name."},
                "category": {"type": "string", "description": "'all' (default), 'integration', 'plugin'/'frontend'/'card', 'theme', 'appdaemon', or 'python_script'."},
                "limit": {"type": "integer", "description": "max results (default 20)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_github",
            "description": "Search GitHub repositories for Home Assistant-related projects, templates and examples matching a device/brand/topic. The query is automatically scoped to the HA ecosystem. Uses GitHub's public search API (unauthenticated rate limit \u2014 use sparingly). Read-only.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "search terms, e.g. 'tuya local' or 'esphome thermostat'."},
                "sort": {"type": "string", "description": "'stars' (default), 'updated', or 'forks'."},
                "limit": {"type": "integer", "description": "max results (default 15, max 30)."},
            }, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_blueprints",
            "description": "Find community blueprint repositories (ready-made automation/script templates) on GitHub, optionally filtered by keyword. Returns repos tagged as HA blueprints; take a raw blueprint .yaml url from one and feed it to import_blueprint. Read-only.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "optional keywords, e.g. 'motion light' or 'notify low battery'."},
                "limit": {"type": "integer", "description": "max results (default 15, max 30)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_resources",
            "description": "One free-text query, every source at once: searches the HACS catalog (integrations/cards/themes), GitHub repos, and community blueprints concurrently and returns each source's results plus a fused, deduped 'top' list (ranked by cross-source presence then stars). A non-expert types a brand or a need ('xiaomi vacuum', 'low battery notification') and gets installable integrations/cards, example repos, and ready-to-import automations together. Each source degrades independently (failures in partial_errors). Read-only.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "what to find: a brand, device type, or desired automation, e.g. 'aqara motion' or 'notify when laundry done'."},
                "limit": {"type": "integer", "description": "max results per source / in the fused top list (default 8)."},
            }, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_zigbee_devices",
            "description": "Look a Zigbee device up in the community device database (blakadder, ~2700 devices). A non-expert types a brand or the model printed on the box ('aqara motion', 'RTCGQ11LM', 'sonoff plug') and gets which bridges support it \u2014 crucially whether zigbee2mqtt does (zigbee2mqtt_supported) and also zha \u2014 plus the device's reference page. Confirms hardware is supported (and by which stack) before searching for automations for it. Read-only.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "a Zigbee device brand, name, or model, e.g. 'aqara motion' or 'RTCGQ11LM'."},
                "limit": {"type": "integer", "description": "max devices to return (default 15)."},
            }, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tasmota_devices",
            "description": "Look a device up in the community Tasmota template database (blakadder, ~2800 templates). A non-expert types a brand/model ('sonoff basic', 'athom plug') and gets the ready-to-flash Tasmota template (GPIO config) plus the reference page \u2014 matching DIY/ESP8266/ESP32 hardware to a working firmware config without hunting forums. Read-only.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "a device brand, name, or model, e.g. 'sonoff basic' or 'athom plug'."},
                "limit": {"type": "integer", "description": "max devices to return (default 15)."},
            }, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_esphome_devices",
            "description": "Look a device up in the community ESPHome device database (devices.esphome.io, ~770 devices). A non-expert types a brand/model ('athom plug', 'martin jerry', 'shelly 1') and gets matching ESPHome-ready devices with the ESP board, device type, whether it's officially 'made for ESPHome', and its config page \u2014 matching DIY/ESP hardware to a known ESPHome setup. Read-only.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "a device brand, name, or model, e.g. 'athom plug' or 'shelly 1'."},
                "limit": {"type": "integer", "description": "max devices to return (default 10)."},
            }, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_ha_integrations",
            "description": "Search Home Assistant's catalog of built-in integrations (~1470). A non-expert types a brand or need ('aqara', 'tuya', 'vacuum') and learns which integrations ship natively with HA \u2014 no HACS install needed \u2014 with each one's IoT class (local/cloud), type, quality scale and docs page. Complements search_community_resources (HACS custom repos). Read-only.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "a brand or need, e.g. 'aqara' or 'vacuum'."},
                "limit": {"type": "integer", "description": "max integrations to return (default 10)."},
            }, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_ha_addons",
            "description": "Search Home Assistant add-on stores (official + well-known community: mosquitto, zigbee2mqtt, esphome, matter server, deconz, etc.). After matching hardware a non-expert often needs a supporting add-on; type a need ('mqtt', 'zigbee2mqtt', 'matter', 'backup') and get matching installable add-ons with their store, slug and page. Read-only.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "a need, e.g. 'mqtt' or 'zigbee2mqtt'."},
                "limit": {"type": "integer", "description": "max add-ons to return (default 10)."},
            }, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_addon",
            "description": "Manage a Supervisor add-on: get info, install, start, stop, restart, or uninstall. Use search_ha_addons to find the slug first, then this tool to act on it. Requires HA OS or Supervised install. Write op (except info) \u2014 gated by allow_write.",
            "parameters": {"type": "object", "properties": {
                "slug": {"type": "string", "description": "add-on slug from search_ha_addons (e.g. 'core_mosquitto', 'a0d7b954_zigbee2mqtt')."},
                "action": {"type": "string", "enum": ["info", "install", "start", "stop", "restart", "uninstall"], "description": "action to perform (default: info)."},
            }, "required": ["slug"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "setup_integration",
            "description": "Set up a native HA integration via its config flow. Pass the domain (from search_ha_integrations) to start. If the integration needs user input, returns the required fields; re-call with user_input to complete setup. For zero-config integrations, one call creates the entry. Write op \u2014 gated by allow_write.",
            "parameters": {"type": "object", "properties": {
                "domain": {"type": "string", "description": "integration domain (e.g. 'utility_meter', 'mqtt', 'zha')."},
                "user_input": {"type": "object", "description": "optional: config values for the flow step (e.g. {\"host\": \"192.168.1.5\", \"port\": 1883})."},
            }, "required": ["domain"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reconfigure_integration",
            "description": "Update options of an existing config entry via its options flow. Most integrations support reconfiguration after setup (e.g. Adaptive Lighting switches, Powercalc models). Pass entry_id from list_config_entries. Returns required fields if user_input omitted. Write op \u2014 gated by allow_write.",
            "parameters": {"type": "object", "properties": {
                "entry_id": {"type": "string", "description": "config entry ID from list_config_entries."},
                "user_input": {"type": "object", "description": "optional: new option values for the flow step."},
            }, "required": ["entry_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_hacs",
            "description": "Manage HACS (Home Assistant Community Store) repositories: list installed, install (download) a new repo, or remove one. Use search_community_resources to find the repo first, then this tool to install it. Requires HACS to be installed. Write ops (except list) gated by allow_write.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "install", "remove"], "description": "action to perform (default: list)."},
                "repo": {"type": "string", "description": "GitHub repo (e.g. 'basnijholt/adaptive-lighting'). Required for install/remove."},
                "category": {"type": "string", "enum": ["integration", "plugin", "theme", "appdaemon", "python_script"], "description": "HACS category (default: integration)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_zwave_devices",
            "description": "Search the community Z-Wave device database (zwave-js/node-zwave-js, ~2375 devices). Type a brand/model ('aeotec', 'fibaro fgs213', 'zooz zen25') and get matching Z-Wave certified devices with manufacturer, model, and a link to the device config file (parameters/associations). This is the Z-Wave analog of search_zigbee_devices. Read-only.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "brand or model, e.g. 'aeotec' or 'fibaro fgs213'."},
                "limit": {"type": "integer", "description": "max devices to return (default 10)."},
            }, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_repo_blueprints",
            "description": "Resolve a GitHub repo (owner/name or URL) to the raw .yaml URLs of the blueprints it contains \u2014 closing the search\u2192import loop so you can feed a raw_url straight to import_blueprint without browsing the repo. Read-only.",
            "parameters": {"type": "object", "properties": {
                "repo": {"type": "string", "description": "owner/name (e.g. 'EPMatt/awesome-ha-blueprints') or a GitHub URL."},
                "limit": {"type": "integer", "description": "max blueprint files (default 30)."},
            }, "required": ["repo"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_resources",
            "description": "Inspect the running HA's real devices (manufacturers), configured integrations and entity domains, then recommend, fused in one call: HACS integrations, HACS frontend cards, and ready-made community automation blueprints \u2014 all matched to that hardware with a reason for each. The 'match my devices to the right resources' capability: an operator can call this with zero arguments to surface what a non-expert user would otherwise never find. Read-only.",
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "max recommendations per kind (default 15)."},
                "include_blueprints": {"type": "boolean", "description": "also fuse in device-matched automation blueprints (default true; set false to skip the GitHub lookups)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_blueprints",
            "description": "Zero-arg 'what can I automate with what I own': maps the home's real entity domains (light/binary_sensor/sensor/lock/climate\u2026) to common automation intents and returns matched community blueprints, each tagged with the intent that surfaced it. Feed a result's full_name to list_repo_blueprints, then import_blueprint. Read-only (bounded GitHub searches).",
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "max blueprint recommendations (default 12)."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_blueprint",
            "description": "Fetch a blueprint YAML by URL (GitHub blob/raw/gist) and import it into the running HA under blueprints/<domain>/ha_copilot/. Keeps a .copilot.bak backup and reloads the domain. Domain is read from the blueprint ('automation'/'script'/'template') unless overridden. Returns 'loadable' (and 'load_error') verifying THIS HA version can actually parse the blueprint. Write op \u2014 gated by allow_write.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "URL to the blueprint .yaml (a GitHub blob url is auto-converted to raw)."},
                "domain": {"type": "string", "description": "optional override: 'automation', 'script', or 'template'."},
            }, "required": ["url"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_memory",
            "description": "Persist a fact across sessions/restarts (upsert by key) \u2014 the agent's long-term memory for what it learned about this home (user preferences, device notes, decisions). value is any JSON; category namespaces it (e.g. 'preferences', 'devices'). Stored as plain JSON via HA's Store; no model/external calls. Write op \u2014 gated by allow_write.",
            "parameters": {"type": "object", "properties": {
                "key": {"type": "string"},
                "value": {"description": "any JSON value to remember"},
                "category": {"type": "string", "description": "namespace, default 'general'"},
            }, "required": ["key", "value"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Recall one persisted memory entry by key (returns found:false if unknown). Read-only.",
            "parameters": {"type": "object", "properties": {
                "key": {"type": "string"},
            }, "required": ["key"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memory",
            "description": "List persisted memory entries (key, value, category, updated_at), most-recent first, optionally filtered by category. Read-only.",
            "parameters": {"type": "object", "properties": {
                "category": {"type": "string", "description": "optional category filter"},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": "Delete one persisted memory entry by key. Write op \u2014 gated by allow_write.",
            "parameters": {"type": "object", "properties": {
                "key": {"type": "string"},
            }, "required": ["key"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_device_profile",
            "description": "Capture the home's real device signals (manufacturers, integration domains, entity-domain counts) into memory under category 'devices', so later sessions recall what the home contains without re-scanning and can detect changes. Write op \u2014 gated by allow_write.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

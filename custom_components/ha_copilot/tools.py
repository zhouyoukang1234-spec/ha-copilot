"""Deep-fusion tool layer: the operations HA-Copilot can perform on Home Assistant.

Each tool is a thin, well-typed wrapper around a Home Assistant internal API
(state machine, service registry, registries, config files, config check). The
LLM agent selects and invokes these via OpenAI-style function calling; this is
the layer that makes the AI "fused" with HA rather than calling it from outside.
"""
from __future__ import annotations

import asyncio
import functools
import os
from datetime import timedelta
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
_DESTRUCTIVE_TOOLS = frozenset({"restart", "purge_recorder", "clear_statistics"})


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
            "description": "List Lovelace dashboards (default + storage + YAML) with their url_path and mode — the UI surface map.",
            "parameters": {"type": "object", "properties": {}},
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
            "name": "add_todo_item",
            "description": "Add an item to a todo list. Defaults to the first todo entity if entity_id is omitted.",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string", "description": "todo.* entity id (optional)."},
                "item": {"type": "string", "description": "The item summary to add."},
            }, "required": ["item"]},
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
            "name": "clear_statistics",
            "description": "Delete all long-term statistics for the given statistic_ids (recorder write) — the cleanup counterpart to import_statistics. Removes the series from history/energy entirely.",
            "parameters": {"type": "object", "properties": {
                "statistic_ids": {"type": "array", "items": {"type": "string"}},
            }, "required": ["statistic_ids"]},
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

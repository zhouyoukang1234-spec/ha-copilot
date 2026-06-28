"""Deep-fusion tool layer: the operations HA-Copilot can perform on Home Assistant.

Each tool is a thin, well-typed wrapper around a Home Assistant internal API
(state machine, service registry, registries, config files, config check). The
LLM agent selects and invokes these via OpenAI-style function calling; this is
the layer that makes the AI "fused" with HA rather than calling it from outside.
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

import voluptuous as vol  # noqa: F401  (kept for future schema validation)
import yaml

from homeassistant.components.recorder import get_instance as _recorder_get_instance
from homeassistant.components.recorder import history as _recorder_history
from homeassistant.components.recorder import statistics as _recorder_statistics
from homeassistant.core import Context, HomeAssistant
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
    await hass.services.async_call(domain, service, data, blocking=True)
    # Let derived entities (e.g. template lights) re-render from their source
    # before we read back the resulting state, so feedback isn't stale.
    await asyncio.sleep(0.2)
    result: dict[str, Any] = {"ok": True, "called": f"{domain}.{service}", "data": data}
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
    from homeassistant.components.lovelace.const import LOVELACE_DATA
    data = hass.data.get(LOVELACE_DATA)
    items = []
    if data is not None:
        for url_path, cfg in data.dashboards.items():
            items.append({
                "url_path": url_path or "lovelace",
                "is_default": url_path is None,
                "mode": getattr(cfg, "mode", None),
            })
        for url_path, ycfg in (data.yaml_dashboards or {}).items():
            items.append({
                "url_path": url_path,
                "title": ycfg.get("title"),
                "icon": ycfg.get("icon"),
                "mode": "yaml",
                "show_in_sidebar": ycfg.get("show_in_sidebar", True),
            })
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
        if name == "assign_entities_by_rules":
            return await _assign_entities_by_rules(
                hass, args["rules"], bool(args.get("only_unassigned", True)))
        if name == "create_helper":
            return await _create_helper(
                hass, store, args["domain"], args["object_id"], args.get("config") or {})
        if name == "create_template_sensor":
            return await _create_template_sensor(
                hass, store, args["name"], args["state"], unit=args.get("unit"),
                device_class=args.get("device_class"), icon=args.get("icon"))
        if name == "list_template_sensors":
            return await _list_template_sensors(hass)
        if name == "create_blueprint_automation":
            return await _create_blueprint_automation(
                hass, args["alias"], args["blueprint_path"], args.get("inputs") or {})
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
                return await _update_automation(hass, ident, new_alias)
            return await _update_script(hass, ident, new_alias)
        if name in ("delete_automation", "delete_scene", "delete_script"):
            if not store.get(CONF_ALLOW_WRITE, True):
                return {"error": "writes are disabled (allow_write: false)"}
            ident = args.get("identifier") or args.get("id") or args.get("name")
            if name == "delete_script":
                ident = ident or args.get("entity_id")
            if not ident:
                return {"error": "missing required argument: identifier"}
            if name == "delete_automation":
                return await _delete_automation(hass, ident)
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
            return await _fire_event(hass, args["event_type"], args.get("event_data"))
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
        return {"error": f"unknown tool '{name}'"}
    except KeyError as err:
        return {"error": f"missing required argument: {err}"}
    except Exception as err:  # noqa: BLE001 - surface any tool failure to the agent
        return {"error": f"{type(err).__name__}: {err}"}


# OpenAI-style function specifications. Exposed to external agents verbatim via
# the run_tool HTTP API and converted to MCP tool descriptors for the MCP server.
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
                    "object_id": {"type": "string", "description": "Helper object id (slug)"},
                    "config": {"type": "object", "description": "Helper config, e.g. {name: '...'}"},
                },
                "required": ["domain", "object_id", "config"],
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
                    "event_data": {"type": "object"},
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
]

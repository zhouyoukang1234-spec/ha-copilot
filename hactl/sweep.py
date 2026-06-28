"""Deterministic sweep of read-only ha_copilot tools against a live HA.

Derives realistic arguments from the running instance, calls each tool via the
HTTP run_tool endpoint, and reports any tool that raises a Python-level error
(traceback / exception) versus an expected validation message. Not committed as
a test gate; a defect-hunting harness used during deployment推演.
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("HA_URL", "http://localhost:8123")
TOKEN = open("/root/ha-copilot/.ha_token").read().strip()


def call(tool, args=None):
    body = json.dumps({"tool": tool, "args": args or {}}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/ha_copilot/run_tool",
        data=body,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        raw = urllib.request.urlopen(req, timeout=30).read().decode()
    except Exception as exc:  # noqa: BLE001
        return {"__http_error__": str(exc)}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"__nonjson__": raw[:300]}
    # Unwrap the {"tool":..,"result":..} envelope so callers see the payload.
    if isinstance(parsed, dict) and "result" in parsed and "tool" in parsed:
        return parsed["result"]
    return parsed


def first(entities, domain=None, attr=None):
    for e in entities:
        eid = e.get("entity_id", "")
        if domain and not eid.startswith(domain + "."):
            continue
        if attr and attr not in (e.get("attributes") or {}):
            continue
        return eid
    return None


# Derive live context
states = call("list_states")
ents = states.get("entities") if isinstance(states, dict) else None
ents = ents or (states if isinstance(states, list) else [])
light = first(ents, "light")
switch = first(ents, "switch")
auto = first(ents, "automation")
scene = first(ents, "scene")
script = first(ents, "script")
person = first(ents, "person")
group = first(ents, "group")
zone = first(ents, "zone")
# statistic-capable sensor (from the recorder statistics catalogue)
stat = None
_stats = call("list_statistics")
_sl = _stats.get("statistics") if isinstance(_stats, dict) else None
if _sl:
    stat = _sl[0].get("statistic_id")

areas = call("list_areas")
area = None
_a = areas.get("areas") if isinstance(areas, dict) else None
if _a:
    area = _a[0].get("area_id") or _a[0].get("id")

ce = call("list_config_entries")
_c = ce.get("entries") if isinstance(ce, dict) else None
entry_id = _c[0].get("entry_id") if _c else None

bl = call("list_blueprints")
_b = bl.get("blueprints") if isinstance(bl, dict) else None
blueprint = None
if _b:
    blueprint = (_b[0].get("path") or _b[0].get("blueprint_path")) if isinstance(_b[0], dict) else _b[0]

# tool name -> args
PLAN = {
    "list_states": {},
    "get_state": {"entity_id": light},
    "list_services": {},
    "list_dir": {"path": "."},
    "read_config_file": {"path": "configuration.yaml"},
    "check_config": {},
    "list_areas": {},
    "registry_overview": {},
    "read_logs": {"lines": 20},
    "render_template": {"template": "{{ states | count }}"},
    "get_history": {"entity_id": light, "hours": 1},
    "list_template_sensors": {},
    "list_blueprints": {},
    "list_backups": {},
    "list_config_entries": {},
    "get_core_config": {},
    "list_entities": {},
    "list_devices": {},
    "list_floors": {},
    "list_labels": {},
    "list_statistics": {},
    "get_statistics": {"statistic_ids": [stat]} if stat else {},
    "list_persons": {},
    "get_logbook": {"hours": 1},
    "list_users": {},
    "list_categories": {"scope": "automation"},
    "list_dashboards": {},
    "get_energy_prefs": {},
    "list_todo_items": {},
    "list_tags": {},
    "get_system_health": {},
    "get_blueprint": {"path": blueprint} if blueprint else {},
    "describe_service": {"domain": "light", "service": "turn_on"},
    "describe_area": {"area_id": area} if area else {},
    "get_entity_registry_entry": {"entity_id": light},
    "get_config_entry": {"entry_id": entry_id} if entry_id else {},
    "get_statistic_metadata": {"statistic_id": stat} if stat else {},
    "evaluate_condition": {"condition": {"condition": "template", "value_template": "{{ true }}"}},
    "list_zones": {},
    "get_system_log": {},
    "get_integration_manifest": {"domain": "light"},
    "get_recorder_info": {},
    "get_loaded_integrations": {},
    "get_automation_config": {"entity_id": auto} if auto else {},
    "validate_automation_config": {"config": {"trigger": [], "action": []}},
    "list_config_flows": {},
    "get_script_config": {"entity_id": script} if script else {},
    "get_scene_config": {"entity_id": scene} if scene else {},
    "get_statistics_during_period": {"statistic_ids": [stat], "hours": 1} if stat else {},
    "get_entity_relations": {"entity_id": light},
    "get_template_functions": {},
    "get_assist_pipelines": {},
    "get_network_adapters": {},
    "get_conversation_agents": {},
    "get_recorder_db_info": {},
    "get_recorder_runs": {},
    "get_entity_sources": {},
    "list_input_helpers": {},
    "list_intents": {},
    "get_group": {"entity_id": group} if group else {},
    "get_person": {"identifier": person} if person else {},
}

# Tools that legitimately return raw log / file content, where a traceback
# string in the payload is data being surfaced, not a tool-level failure.
CONTENT_TOOLS = {"read_logs", "get_system_log", "get_logbook", "read_config_file"}

errs = []
ok = 0
skipped = []
for tool, args in PLAN.items():
    if not args and tool in (
        "get_statistics", "get_blueprint", "describe_area", "get_config_entry",
        "get_statistic_metadata", "get_automation_config", "get_script_config",
        "get_scene_config", "get_statistics_during_period", "get_group", "get_person",
    ):
        skipped.append(tool + "(no live arg)")
        continue
    res = call(tool, args)
    blob = json.dumps(res, ensure_ascii=False)
    transport_bad = "__http_error__" in res or "__nonjson__" in res
    if tool in CONTENT_TOOLS:
        bad = transport_bad
    else:
        bad = (
            transport_bad
            or "Traceback" in blob or "AttributeError" in blob
            or "ImportError" in blob or "KeyError" in blob
            or "TypeError" in blob or "has no attribute" in blob
        )
    if bad:
        errs.append((tool, blob[:280]))
    else:
        ok += 1

print(f"SWEEP: ok={ok} errors={len(errs)} skipped={len(skipped)} of planned={len(PLAN)}")
for t, e in errs:
    print(f"  [ERR] {t}: {e}")
if skipped:
    print("  skipped:", ", ".join(skipped))
sys.exit(1 if errs else 0)

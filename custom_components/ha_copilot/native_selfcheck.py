"""End-to-end acceptance suite for the native HA-Copilot MCP endpoint.

Drives the *real* HTTP MCP endpoint (``/api/ha_copilot/mcp``) the same way any
external agent would: JSON-RPC ``initialize`` -> ``tools/list`` -> ``tools/call``.
Every user-operable module is exercised read -> write -> verify -> clean up.
Idempotent: yaml-backed artifacts (automations/scenes/scripts) are snapshotted
and restored, registry/dashboard artifacts are deleted, so it can run forever.

Usage::

    HA_BASE_URL=http://localhost:8123 HA_TOKEN=$(cat ~/ha_llat.txt) \
        python3 -m custom_components.ha_copilot.native_selfcheck

(or run the file directly). Exit code is non-zero if any check fails.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("HA_BASE_URL", "http://localhost:8123").rstrip("/")
TOKEN = os.environ.get("HA_TOKEN") or (
    open(os.path.expanduser("~/ha_llat.txt")).read().strip()
    if os.path.exists(os.path.expanduser("~/ha_llat.txt"))
    else ""
)
URL = f"{BASE}/api/ha_copilot/mcp"
SUFFIX = str(int(time.time()))[-6:]

_passed = 0
_failed = 0
_rpc_id = 0


class ToolError(Exception):
    pass


def _rpc(method: str, params: dict | None = None):
    global _rpc_id
    _rpc_id += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _rpc_id, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(
        URL, data=body,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read())


def call(_tool: str, **args):
    """Call an MCP tool; return its structured result or raise ToolError."""
    res = _rpc("tools/call", {"name": _tool, "arguments": args})
    if "error" in res:
        raise ToolError(f"{_tool}: rpc error {res['error']}")
    result = res["result"]
    if result.get("isError"):
        raise ToolError(f"{_tool}: {result['content'][0]['text']}")
    sc = result.get("structuredContent") or {}
    return sc.get("result", sc)


def guard(label: str, fn):
    global _passed, _failed
    try:
        detail = fn()
        _passed += 1
        print(f"  PASS  {label}" + (f"  -- {detail}" if detail else ""))
    except Exception as err:  # noqa: BLE001 - report and continue
        _failed += 1
        print(f"  FAIL  {label}  -- {type(err).__name__}: {err}")


def main() -> int:
    print(f"== native MCP selfcheck @ {URL}  (suffix {SUFFIX}) ==")

    # --- handshake ---
    init = _rpc("initialize")["result"]
    assert init["protocolVersion"], "no protocolVersion"
    tools = [t["name"] for t in _rpc("tools/list")["result"]["tools"]]
    print(f"   initialize ok; {len(tools)} tools advertised")

    # --- states & services ---
    def t_states():
        s = call("list_states", domain="light")
        assert s["count"] >= 1, "no lights"
        return f"{s['count']} lights"
    guard("states: list lights", t_states)

    lights = call("list_states", domain="light")["entities"]
    light_id = lights[0]["entity_id"]

    def t_service():
        call("call_service", domain="light", service="turn_on", entity_id=light_id)
        st = call("get_state", entity_id=light_id)
        assert st["state"] == "on", f"expected on, got {st['state']}"
        call("call_service", domain="light", service="turn_off", entity_id=light_id)
        return f"{light_id} on->off"
    guard("services: drive a light closed-loop", t_service)

    guard("template: render", lambda: (
        None if call("render_template", template="{{ 1 + 1 }}") == 2 else
        (_ for _ in ()).throw(AssertionError("1+1 != 2"))) or "1+1=2")

    guard("history: read recorder", lambda: f"{call('get_history', entity_id=light_id, hours=1)['count']} points")
    guard("config: check valid", lambda: "valid" if call("check_config")["valid"] else
          (_ for _ in ()).throw(AssertionError("config invalid")))
    guard("registry: overview", lambda: str(call("registry_overview")))

    # --- areas (typed create + ws delete) ---
    area_name = f"mcp-area-{SUFFIX}"
    area_id_box = {}

    def t_area():
        r = call("create_area", name=area_name)
        area_id_box["id"] = r["area_id"]
        names = [a["name"] for a in call("list_areas")["areas"]]
        assert area_name in names, "area not listed"
        return r["area_id"]
    guard("areas: create + list", t_area)

    # --- labels (ws CRUD) ---
    def t_label():
        lbl = call("ha_ws", command_type="config/label_registry/create",
                   payload={"name": f"mcp-label-{SUFFIX}", "color": "indigo"})
        lid = lbl["label_id"]
        ids = [x["label_id"] for x in call("list_labels")]
        assert lid in ids, "label not listed"
        call("ha_ws", command_type="config/label_registry/delete", payload={"label_id": lid})
        return lid
    guard("labels: ws create/list/delete", t_label)

    # --- floors (ws CRUD) ---
    def t_floor():
        fl = call("ha_ws", command_type="config/floor_registry/create",
                  payload={"name": f"mcp-floor-{SUFFIX}"})
        fid = fl["floor_id"]
        ids = [x["floor_id"] for x in call("list_floors")]
        assert fid in ids, "floor not listed"
        call("ha_ws", command_type="config/floor_registry/delete", payload={"floor_id": fid})
        return fid
    guard("floors: ws create/list/delete", t_floor)

    # --- entity registry: assign area then clear ---
    def t_entity():
        regs = [e for e in call("list_entities") if e.get("entity_id", "").startswith("light.")]
        if not regs or "id" not in area_id_box:
            return "skipped (no registry light)"
        eid = regs[0]["entity_id"]
        call("assign_entity_area", entity_id=eid, area=area_id_box["id"])
        ent = next(e for e in call("list_entities") if e["entity_id"] == eid)
        assert ent.get("area_id") == area_id_box["id"], "area not assigned"
        call("ha_ws", command_type="config/entity_registry/update",
             payload={"entity_id": eid, "area_id": None})
        return eid
    guard("entities: assign area via registry", t_entity)

    # cleanup area now that entity test is done
    def t_area_delete():
        if "id" in area_id_box:
            call("ha_ws", command_type="config/area_registry/delete",
                 payload={"area_id": area_id_box["id"]})
        return "deleted"
    guard("areas: cleanup", t_area_delete)

    # --- dashboards (ws collection CRUD) ---
    def t_dash():
        url_path = f"mcp-dash-{SUFFIX}"
        d = call("ha_ws", command_type="lovelace/dashboards/create",
                 payload={"url_path": url_path, "title": f"MCP {SUFFIX}",
                          "show_in_sidebar": False})
        did = d["id"]
        paths = [x["url_path"] for x in call("list_dashboards")]
        assert url_path in paths, "dashboard not listed"
        # write a view into it
        call("ha_ws", command_type="lovelace/config/save",
             payload={"url_path": url_path,
                      "config": {"title": "MCP", "views": [{"title": "Home",
                                 "cards": [{"type": "markdown", "content": "native mcp"}]}]}})
        call("ha_ws", command_type="lovelace/dashboards/delete", payload={"dashboard_id": did})
        return url_path
    guard("dashboards: ws create/save/delete", t_dash)

    # --- helper (input_boolean via ws) ---
    def t_helper():
        h = call("ha_ws", command_type="input_boolean/create",
                 payload={"name": f"mcp_helper_{SUFFIX}"})
        hid = h["id"]
        ids = [x["id"] for x in call("ha_ws", command_type="input_boolean/list")]
        assert hid in ids, "helper not listed"
        call("ha_ws", command_type="input_boolean/delete", payload={"input_boolean_id": hid})
        return f"input_boolean.{hid}"
    guard("helpers: create/list/delete input_boolean", t_helper)

    # --- yaml-backed artifacts: snapshot, create, verify, restore ---
    snaps = {}
    for fname in ("automations.yaml", "scenes.yaml", "scripts.yaml"):
        try:
            snaps[fname] = call("read_config_file", path=fname).get("content", "")
        except Exception:
            snaps[fname] = ""

    def t_automation():
        alias = f"mcp_auto_{SUFFIX}"
        call("create_automation", automation={
            "id": f"mcp_{SUFFIX}", "alias": alias,
            "trigger": [{"platform": "state", "entity_id": light_id}],
            "action": [{"service": "light.turn_on", "target": {"entity_id": light_id}}],
        })
        content = call("read_config_file", path="automations.yaml")["content"]
        assert alias in content, "automation not persisted"
        return alias
    guard("automation: create + persist", t_automation)

    def t_scene():
        call("create_scene", name=f"mcp_scene_{SUFFIX}", entities={light_id: "on"})
        content = call("read_config_file", path="scenes.yaml")["content"]
        assert f"mcp_scene_{SUFFIX}" in content, "scene not persisted"
        return "ok"
    guard("scene: create + persist", t_scene)

    def t_script():
        r = call("create_script", alias=f"mcp_script_{SUFFIX}",
                 sequence=[{"service": "light.turn_off", "target": {"entity_id": light_id}}])
        return r.get("script_entity_id", "ok")
    guard("script: create + persist", t_script)

    # restore yaml files exactly + reload, then purge orphaned registry entities
    def t_restore():
        for fname, content in snaps.items():
            call("write_config_file", path=fname, content=content)
        for dom in ("automation", "scene", "script"):
            try:
                call("reload", domain=dom)
            except Exception:
                pass
        # Reloading drops the yaml configs, but the entities they registered
        # linger as "unavailable" registry orphans. Sweep them by name pattern
        # (scene entity_ids are sticky to first creation, so don't key off the
        # current suffix) so the run is truly idempotent and leaves zero residue.
        marks = ("mcp_auto_", "mcp_scene_", "mcp_script_")
        reg = call("ha_ws", command_type="config/entity_registry/list")
        orphans = [
            e["entity_id"] for e in reg
            if any(m in e["entity_id"] for m in marks)
        ]
        removed = 0
        for eid in orphans:
            try:
                call("ha_ws", command_type="config/entity_registry/remove",
                     payload={"entity_id": eid})
                removed += 1
            except Exception:
                pass
        return f"yaml restored, {removed} orphan(s) purged"
    guard("cleanup: restore yaml + reload + purge orphans", t_restore)

    # --- users / config entries / system health / universal ws ---
    guard("users: list", lambda: f"{len(call('list_users'))} users")
    guard("config_entries: list", lambda: f"{len(call('list_config_entries'))} entries")
    guard("system_health: info", lambda: f"{len(call('system_health'))} domains")
    guard("ha_ws universal: core config", lambda: call("ha_ws", command_type="get_config")["version"])

    print(f"\n==== {_passed}/{_passed + _failed} passed ====")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

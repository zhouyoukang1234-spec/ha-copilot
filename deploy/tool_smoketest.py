"""Deterministic closed-loop test of every HA-Copilot tool via run_tool service."""
import json
import os
import urllib.request

BASE = os.environ.get("HA_BASE", "http://localhost:8123")
TOKEN = (
    os.environ.get("HA_TOKEN")
    or open(os.environ.get("HA_TOKEN_FILE", "ha_token.txt")).read().strip()
)


def call(path, body=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())


def tool(name, args=None):
    out = call("/api/services/ha_copilot/run_tool?return_response=true",
               {"tool": name, "args": args or {}})
    # service_response holds the tool's dict
    return out.get("service_response", out)


def state(eid):
    try:
        return call(f"/api/states/{eid}", method="GET")["state"]
    except Exception as e:
        return f"<err {e}>"


results = []


def check(num, name, ok, detail=""):
    results.append((num, name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} #{num} {name}: {detail}")


LIVING = "light.ke_ting_deng"
BEDROOM = "light.wo_shi_deng"

# 1 list_states
r = tool("list_states", {"domain": "light"})
check(1, "list_states", r.get("count", 0) >= 3, f"count={r.get('count')}")

# 2 get_state
r = tool("get_state", {"entity_id": LIVING})
check(2, "get_state", r.get("entity_id") == LIVING, f"state={r.get('state')}")

# 3 list_services
r = tool("list_services", {"domain": "light"})
check(3, "list_services", "turn_on" in r.get("services", []), f"{r.get('services')}")

# 4 call_service turn_on
r = tool("call_service", {"domain": "light", "service": "turn_on", "entity_id": LIVING})
check(4, "call_service turn_on", state(LIVING) == "on", f"living={state(LIVING)} resp_states={r.get('states')}")

# 5 render_template
r = tool("render_template", {"template": "{{ states('" + LIVING + "') }}"})
check(5, "render_template", r.get("result") == "on", f"result={r.get('result')}")

# 6 create_area
r = tool("create_area", {"name": "测试客厅"})
area_id = r.get("area_id")
check(6, "create_area", bool(area_id), f"area_id={area_id}")

# 7 assign_entity_area
r = tool("assign_entity_area", {"entity_id": LIVING, "area": "测试客厅"})
check(7, "assign_entity_area", r.get("area_id") == area_id, f"area_id={r.get('area_id')}")

# 8 rename_entity
r = tool("rename_entity", {"entity_id": LIVING, "name": "客厅主灯"})
check(8, "rename_entity", r.get("name") == "客厅主灯", f"name={r.get('name')}")

# 9 set_entity_enabled (disable then re-enable)
r1 = tool("set_entity_enabled", {"entity_id": BEDROOM, "enabled": False})
r2 = tool("set_entity_enabled", {"entity_id": BEDROOM, "enabled": True})
check(9, "set_entity_enabled roundtrip", r1.get("disabled") is True and r2.get("disabled") is False,
      f"disable={r1.get('disabled')} enable={r2.get('disabled')}")

# 10 list_areas
r = tool("list_areas")
check(10, "list_areas", any(a["id"] == area_id for a in r.get("areas", [])), f"n={len(r.get('areas', []))}")

# 11 registry_overview
r = tool("registry_overview")
check(11, "registry_overview", r.get("entities", 0) > 0, str(r))

# 12 write_config_file + 13 read_config_file
r = tool("write_config_file", {"path": "copilot_test.txt", "content": "hello-copilot"})
r2 = tool("read_config_file", {"path": "copilot_test.txt"})
check(12, "write_config_file", r.get("ok") is True, str(r))
check(13, "read_config_file", r2.get("content") == "hello-copilot", f"content={r2.get('content')}")

# 14 check_config
r = tool("check_config")
check(14, "check_config", r.get("valid") is True, str(r)[:120])

# 15 create_scene
r = tool("create_scene", {"name": "观影模式", "entities": {LIVING: "on", BEDROOM: "off"}})
check(15, "create_scene", r.get("ok") is True, str(r))

# 16 create_script
r = tool("create_script", {"alias": "关闭客厅灯", "sequence": [
    {"service": "light.turn_off", "target": {"entity_id": LIVING}}]})
script_eid = r.get("script_entity_id")
check(16, "create_script", bool(script_eid), f"eid={script_eid}")

# 17 reload (script already reloaded; test reload of input_boolean? use 'template')
r = tool("reload", {"domain": "script"})
check(17, "reload", r.get("ok") is True, str(r))

# 18 execute AI-created script -> turns living off
tool("call_service", {"domain": "light", "service": "turn_on", "entity_id": LIVING})
if script_eid:
    tool("call_service", {"domain": "script", "service": "turn_on", "entity_id": script_eid})
check(18, "execute created script turns light off", state(LIVING) == "off", f"living={state(LIVING)}")

# 19 create_automation
r = tool("create_automation", {"automation": {
    "alias": "copilot test auto",
    "trigger": [{"platform": "state", "entity_id": LIVING, "to": "on"}],
    "action": [{"service": "light.turn_on", "target": {"entity_id": BEDROOM}}],
}})
check(19, "create_automation", r.get("ok") is True, str(r))

# 20 get_history
r = tool("get_history", {"entity_id": LIVING, "hours": 1})
check(20, "get_history", "count" in r, f"count={r.get('count')} err={r.get('error')}")

# 21 read_logs
r = tool("read_logs", {"lines": 5})
check(21, "read_logs", "log_tail" in r or "error" in r, f"keys={list(r.keys())}")

# 22 safety: path escape must be refused
r = tool("read_config_file", {"path": "../../etc/passwd"})
check(22, "path-escape refused", "error" in r, str(r)[:80])

# 23 unknown tool
r = tool("unknown_tool_xyz")
check(23, "unknown tool errors", "error" in r, str(r)[:60])

n_pass = sum(1 for _, _, ok, _ in results if ok)
print(f"\n==== {n_pass}/{len(results)} passed ====")

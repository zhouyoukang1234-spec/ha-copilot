"""Drive the HA-MCP server end-to-end over the real MCP stdio protocol.

This is the agent (us) actually *using* the bridge: it spawns ``ha_mcp.server``
as an MCP subprocess, speaks MCP to it, and exercises every user-operable module
of a live Home Assistant - reading, writing, verifying the closed loop and
cleaning up. Each step prints PASS / FAIL so defects surface in practice.

Run with HA_BASE_URL and HA_TOKEN set in the environment.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PASS = 0
FAIL = 0
RESULTS: list[str] = []


def _record(ok: bool, name: str, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        RESULTS.append(f"PASS  {name}  {detail}")
    else:
        FAIL += 1
        RESULTS.append(f"FAIL  {name}  {detail}")
    print(RESULTS[-1], flush=True)


class Bridge:
    def __init__(self, session: ClientSession):
        self.s = session

    async def call(self, tool: str, **args):
        res = await self.s.call_tool(tool, args)
        if res.isError:
            text = res.content[0].text if res.content else ""
            raise RuntimeError(f"{tool} error: {text}")
        # Typed-return tools expose structuredContent (FastMCP wraps non-dict
        # returns under a single "result" key); Any-typed tools leave it null and
        # emit one text block per item, so reconstruct from the text blocks.
        sc = res.structuredContent
        if isinstance(sc, dict):
            if set(sc.keys()) == {"result"}:
                return sc["result"]
            return sc
        blocks = [c.text for c in res.content if hasattr(c, "text")]
        parsed = []
        for t in blocks:
            try:
                parsed.append(json.loads(t))
            except (json.JSONDecodeError, TypeError):
                parsed.append(t)
        # FastMCP conveys an empty sequence as no structured data and zero
        # content blocks; read that as an empty list.
        if not parsed:
            return []
        return parsed[0] if len(parsed) == 1 else parsed


async def guard(name: str, coro, predicate=lambda r: True, detail=lambda r: "") -> Any:
    """Run one closed-loop step in isolation: never abort the suite, record
    PASS/FAIL from ``predicate`` (or the raised exception)."""
    try:
        res = await coro
        _record(bool(predicate(res)), name, detail(res))
        return res
    except Exception as e:  # noqa: BLE001
        _record(False, name, str(e)[:140])
        return None


async def run(b: Bridge) -> None:
    suffix = str(int(time.time()))[-6:]

    # ---- overview / discovery
    await guard("ha_overview", b.call("ha_overview"),
                lambda r: r.get("state_count", 0) > 0,
                lambda r: f"v{r.get('version')} states={r.get('state_count')} areas={r.get('area_count')}")
    await guard("list_services", b.call("list_services"),
                lambda r: isinstance(r, list) and "light" in r, lambda r: f"{len(r)} domains")

    # ---- states + services closed loop
    lights = await b.call("list_states", domain="light")
    eid = lights[0]["entity_id"] if lights else None
    _record(bool(eid), "list_states(light)", f"{len(lights)} lights")
    if eid:
        async def turn(service, want):
            await b.call("call_service", domain="light", service=service,
                         target=json.dumps({"entity_id": eid}))
            # MQTT-backed entities echo their new state asynchronously, so poll
            # for convergence instead of reading once immediately (which races
            # the command round-trip and yields the stale state).
            state = None
            for _ in range(20):
                state = (await b.call("get_state", entity_id=eid))["state"]
                if state == want:
                    break
                await asyncio.sleep(0.25)
            return state
        await guard("call_service light.turn_on", turn("turn_on", "on"),
                    lambda s: s == "on", lambda s: f"{eid}={s}")
        await guard("call_service light.turn_off", turn("turn_off", "off"),
                    lambda s: s == "off", lambda s: f"{eid}={s}")

    # ---- template / set_state / history / error log
    await guard("render_template", b.call("render_template", template="{{ 6*7 }}"),
                lambda r: r == "42", lambda r: f"6*7={r}")
    await b.call("set_state", entity_id="sensor.mcp_probe", state="alive",
                 attributes=json.dumps({"unit_of_measurement": "x"}))
    await guard("set_state", b.call("get_state", entity_id="sensor.mcp_probe"),
                lambda r: r["state"] == "alive", lambda r: "sensor.mcp_probe=alive")
    await guard("get_history", b.call("get_history", entity_id=eid or "sensor.mcp_probe", hours=1),
                lambda r: isinstance(r, list), lambda r: f"{len(r)} series")
    await guard("get_error_log", b.call("get_error_log"),
                lambda r: isinstance(r, str), lambda r: f"{len(r)} bytes")

    # ---- areas / floors / devices / entity registry
    floor = await guard("create_floor", b.call("create_floor", name=f"Floor {suffix}", level=2),
                        lambda r: bool(r.get("floor_id")), lambda r: r.get("floor_id"))
    area = await b.call("create_area", name=f"MCP_Area_{suffix}")
    aid = area["area_id"]
    await b.call("update_area", area_id=aid, changes=json.dumps({"name": f"MCP_Area_{suffix}_v2"}))
    if eid:
        await b.call("update_entity", entity_id=eid, changes=json.dumps({"area_id": aid}))
        await guard("entity area assign", b.call("get_entity", entity_id=eid),
                    lambda r: r.get("area_id") == aid, lambda r: f"{eid}->{aid}")
        await b.call("update_entity", entity_id=eid, changes=json.dumps({"area_id": None}))
    await b.call("delete_area", area_id=aid)
    if floor:
        await b.call("ha_ws", command_type="config/floor_registry/delete",
                     payload=json.dumps({"floor_id": floor["floor_id"]}))
    await guard("area create/update/delete", b.call("list_areas"),
                lambda r: all(a["area_id"] != aid for a in r), lambda r: aid)
    await guard("list_devices", b.call("list_devices"), lambda r: isinstance(r, list),
                lambda r: f"{len(r)} devices")

    # ---- labels (unique name per run)
    label = await guard("create_label", b.call("create_label", name=f"mcp-{suffix}", color="indigo"),
                        lambda r: bool(r.get("label_id")), lambda r: r.get("label_id"))

    # ---- helper create + drive
    async def helper_flow():
        h = await b.call("create_helper", helper_domain="input_boolean",
                         config=json.dumps({"name": f"MCP Flag {suffix}"}))
        helper_eid = "input_boolean." + h.get("id", "")
        await b.call("call_service", domain="input_boolean", service="turn_on",
                     target=json.dumps({"entity_id": helper_eid}))
        return helper_eid, (await b.call("get_state", entity_id=helper_eid))["state"], h.get("id")
    hres = await guard("create_helper + drive", helper_flow(),
                       lambda r: r[1] == "on", lambda r: f"{r[0]}={r[1]}")

    # ---- automation create -> verify (read config back, deterministic) -> delete
    auto_id = "9900001"
    async def auto_flow():
        await b.call("save_automation", config_id=auto_id, config=json.dumps({
            "alias": "MCP Probe Automation",
            "trigger": [{"platform": "state", "entity_id": "sensor.mcp_probe"}],
            "action": [{"service": "system_log.write", "data": {"message": "mcp probe fired"}}],
        }))
        return await b.call("get_automation", config_id=auto_id)
    await guard("save_automation", auto_flow(),
                lambda r: r.get("alias") == "MCP Probe Automation", lambda r: f"id={auto_id}")
    await b.call("delete_automation", config_id=auto_id)
    async def del_check():
        try:
            r = await b.call("ha_rest", method="GET", path=f"/api/config/automation/config/{auto_id}")
            return not (isinstance(r, dict) and "alias" in r)
        except RuntimeError as e:
            return "404" in str(e)  # gone -> deleted
    await guard("delete_automation", del_check(), lambda r: r, lambda r: auto_id)

    # ---- scene create + verify (read config back)
    async def scene_flow():
        await b.call("save_scene", scene_id="9900002", config=json.dumps({
            "name": "MCP Probe Scene", "entities": {eid: "on"} if eid else {},
        }))
        return await b.call("get_scene", scene_id="9900002")
    await guard("save_scene", scene_flow(),
                lambda r: r.get("name") == "MCP Probe Scene", lambda r: "scene.9900002")

    # ---- script create + verify (read config back)
    async def script_flow():
        await b.call("save_script", object_id="mcp_probe_script", config=json.dumps({
            "alias": "MCP Probe Script",
            "sequence": [{"service": "system_log.write", "data": {"message": "mcp script ran"}}],
        }))
        return await b.call("get_script", object_id="mcp_probe_script")
    await guard("save_script", script_flow(),
                lambda r: r.get("alias") == "MCP Probe Script", lambda r: "script.mcp_probe_script")

    # ---- dashboards: create -> set config -> verify -> delete
    async def dash_flow():
        url = f"mcp-{suffix}"
        await b.call("ha_ws", command_type="lovelace/dashboards/create", payload=json.dumps({
            "url_path": url, "title": f"MCP {suffix}", "show_in_sidebar": False,
            "require_admin": False, "mode": "storage", "icon": "mdi:robot",
        }))
        await b.call("save_dashboard", url_path=url, config=json.dumps({
            "views": [{"title": "Probe", "cards": [{"type": "markdown", "content": "driven by HA-MCP"}]}],
        }))
        cfg = await b.call("get_dashboard", url_path=url)
        dashes = await b.call("list_dashboards")
        return url, cfg, dashes
    dres = await guard("dashboard create/save/get", dash_flow(),
                       lambda r: r[1]["views"][0]["title"] == "Probe", lambda r: f"{len(r[2])} dashboards")

    # ---- users / integrations / system
    await guard("list_users", b.call("list_users"),
                lambda r: isinstance(r, list) and len(r) >= 1, lambda r: f"{len(r)} users")
    await guard("list_config_entries", b.call("list_config_entries"),
                lambda r: isinstance(r, list), lambda r: f"{len(r)} integrations")
    await guard("system_health", b.call("system_health"),
                lambda r: isinstance(r, dict), lambda r: f"{len(r)} sections")

    # ---- cleanup the artifacts this run created (idempotent reruns)
    if label:
        await guard("cleanup label", b.call("ha_ws", command_type="config/label_registry/delete",
                    payload=json.dumps({"label_id": label["label_id"]})), lambda r: True)
    if hres and hres[2]:
        await guard("cleanup helper", b.call("ha_ws", command_type="input_boolean/delete",
                    payload=json.dumps({"input_boolean_id": hres[2]})), lambda r: True)
    await b.call("ha_rest", method="DELETE", path="/api/config/scene/config/9900002")
    await b.call("ha_rest", method="DELETE", path="/api/config/script/config/mcp_probe_script")
    await b.call("ha_rest", method="DELETE", path="/api/states/sensor.mcp_probe")
    if dres:
        dlist = dres[2]
        did = next((d["id"] for d in dlist if d.get("url_path") == dres[0]), None)
        if did:
            await b.call("ha_ws", command_type="lovelace/dashboards/delete",
                         payload=json.dumps({"dashboard_id": did}))

    # ---- logbook / config check / escape hatches
    await guard("get_logbook", b.call("get_logbook", hours=24),
                lambda r: isinstance(r, list), lambda r: f"{len(r)} entries")
    await guard("check_config", b.call("check_config"),
                lambda r: r.get("result") == "valid", lambda r: r.get("result"))
    await guard("ha_rest escape hatch", b.call("ha_rest", method="GET", path="/api/"),
                lambda r: isinstance(r, (dict, str)), lambda r: "/api/")
    await guard("ha_ws escape hatch", b.call("ha_ws", command_type="get_config"),
                lambda r: isinstance(r, dict) and "version" in r, lambda r: "get_config")


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "ha_mcp.server"],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"== HA-MCP exposes {len(tools.tools)} tools ==", flush=True)
            await run(Bridge(session))
    print("\n".join(RESULTS))
    print(f"\n==== {PASS}/{PASS + FAIL} passed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

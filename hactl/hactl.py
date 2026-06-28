#!/usr/bin/env python3
"""hactl - a bottom-layer Home Assistant control surface, built for an AI operator.

This is *my* (the agent's) infrastructure: a single, scriptable command line that
exposes Home Assistant's bottom layer directly, so I can operate the instance
full-chain without a human or a weak intermediary model in the loop.

Every command prints a single JSON object to stdout, so results are trivially
machine-readable and chainable. Coverage:

  state plane    : states, get, call, template, history, services, error-log, config
  registry plane : areas, area-create, entities, entity, entity-update,
                   devices, device-update, labels, label-create
  config plane   : automation-list/create/delete, scene-create, script-create,
                   check, reload
  raw config     : conf-get, conf-set   (direct YAML file read/write)

Token: read from $HA_TOKEN, else from <repo>/.ha_token (see bootstrap_token.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

import requests
import websockets

BASE = os.environ.get("HA_BASE", "http://localhost:8123")
WS = BASE.replace("http", "ws", 1) + "/api/websocket"
_TOKEN_FILE = pathlib.Path(__file__).resolve().parent.parent / ".ha_token"


def _token() -> str:
    tok = os.environ.get("HA_TOKEN")
    if tok:
        return tok.strip()
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text(encoding="utf-8").strip()
    raise SystemExit("no token: set $HA_TOKEN or run bootstrap_token.py")


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def out(obj) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ----------------------------------------------------------------------------
# REST helpers (state plane)
# ----------------------------------------------------------------------------
def rest(method: str, path: str, body=None, params=None):
    r = requests.request(
        method, f"{BASE}{path}", headers=_headers(), json=body, params=params, timeout=60
    )
    r.raise_for_status()
    if r.text:
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}
    return {}


# ----------------------------------------------------------------------------
# WebSocket helper (registry plane) - one request/response per call
# ----------------------------------------------------------------------------
async def _ws_call(msgs: list[dict]) -> list:
    """Authenticate, send each message in order, return their results."""
    results = []
    async with websockets.connect(WS, max_size=None) as ws:
        assert json.loads(await ws.recv())["type"] == "auth_required"
        await ws.send(json.dumps({"type": "auth", "access_token": _token()}))
        if json.loads(await ws.recv())["type"] != "auth_ok":
            raise SystemExit("ws auth failed")
        mid = 0
        for m in msgs:
            mid += 1
            m = {"id": mid, **m}
            await ws.send(json.dumps(m))
            while True:
                resp = json.loads(await ws.recv())
                if resp.get("id") == mid and resp.get("type") == "result":
                    if not resp.get("success", False):
                        raise SystemExit(f"ws cmd failed: {m} -> {resp.get('error')}")
                    results.append(resp.get("result"))
                    break
    return results


def ws(msg: dict):
    return asyncio.run(_ws_call([msg]))[0]


def ws_many(msgs: list[dict]):
    return asyncio.run(_ws_call(msgs))


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
def cmd_states(a):
    data = rest("GET", "/api/states")
    if a.domain:
        data = [s for s in data if s["entity_id"].startswith(a.domain + ".")]
    if a.brief:
        out([{"entity_id": s["entity_id"], "state": s["state"]} for s in data])
    else:
        out(data)


def cmd_get(a):
    out(rest("GET", f"/api/states/{a.entity_id}"))


def cmd_call(a):
    domain, service = a.service.split(".", 1)
    data = json.loads(a.data) if a.data else {}
    path = f"/api/services/{domain}/{service}"
    if a.response:
        path += "?return_response"
    out(rest("POST", path, data) or {"ok": True})


def cmd_template(a):
    res = requests.post(
        f"{BASE}/api/template", headers=_headers(), json={"template": a.template}, timeout=60
    )
    res.raise_for_status()
    out({"result": res.text})


def cmd_history(a):
    import datetime as _dt

    start = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=a.hours)).isoformat()
    data = rest(
        "GET",
        f"/api/history/period/{start}",
        params={"filter_entity_id": a.entity_id, "minimal_response": "true"},
    )
    out(data)


def cmd_services(a):
    data = rest("GET", "/api/services")
    if a.domain:
        data = [d for d in data if d["domain"] == a.domain]
    out(data)


def cmd_errorlog(a):
    r = requests.get(f"{BASE}/api/error_log", headers=_headers(), timeout=60)
    out({"log": r.text[-8000:]})


def cmd_config(a):
    out(rest("GET", "/api/config"))


def cmd_check(a):
    out(rest("POST", "/api/config/core/check_config"))


def cmd_reload(a):
    domain = a.domain or "all"
    if domain == "all":
        out(rest("POST", "/api/services/homeassistant/reload_all", {}) or {"ok": True})
    else:
        out(rest("POST", f"/api/services/{domain}/reload", {}) or {"ok": True})


# --- registry plane -------------------------------------------------------
def cmd_areas(a):
    out(ws({"type": "config/area_registry/list"}))


def cmd_area_create(a):
    existing = ws({"type": "config/area_registry/list"})
    for area in existing:
        if area["name"] == a.name:
            out({**area, "existed": True})
            return
    out({**ws({"type": "config/area_registry/create", "name": a.name}), "existed": False})


def cmd_entities(a):
    data = ws({"type": "config/entity_registry/list"})
    if a.domain:
        data = [e for e in data if e["entity_id"].startswith(a.domain + ".")]
    out(data)


def cmd_entity(a):
    out(ws({"type": "config/entity_registry/get", "entity_id": a.entity_id}))


def cmd_entity_update(a):
    msg = {"type": "config/entity_registry/update", "entity_id": a.entity_id}
    if a.name is not None:
        msg["name"] = a.name
    if a.area is not None:
        msg["area_id"] = a.area or None
    if a.new_id is not None:
        msg["new_entity_id"] = a.new_id
    if a.disabled is not None:
        msg["disabled_by"] = "user" if a.disabled == "true" else None
    out(ws(msg))


def cmd_devices(a):
    out(ws({"type": "config/device_registry/list"}))


def cmd_device_update(a):
    msg = {"type": "config/device_registry/update", "device_id": a.device_id}
    if a.area is not None:
        msg["area_id"] = a.area or None
    if a.name is not None:
        msg["name_by_user"] = a.name
    out(ws(msg))


# --- config-editor plane (writes YAML like the HA UI) ---------------------
def cmd_automation_list(a):
    # The config editor has no list endpoint; enumerate live automation entities.
    states = rest("GET", "/api/states")
    out(
        [
            {
                "entity_id": s["entity_id"],
                "name": s["attributes"].get("friendly_name"),
                "state": s["state"],
            }
            for s in states
            if s["entity_id"].startswith("automation.")
        ]
    )


def cmd_automation_create(a):
    body = json.loads(a.json)
    aid = a.id
    res = rest("POST", f"/api/config/automation/config/{aid}", body)
    rest("POST", "/api/services/automation/reload", {})
    out({"ok": True, "id": aid, "result": res})


def _entity_by_attr(domain: str, attr: str, value) -> str | None:
    import time

    for _ in range(10):
        for s in rest("GET", "/api/states"):
            if s["entity_id"].startswith(domain + ".") and s["attributes"].get(attr) == value:
                return s["entity_id"]
        time.sleep(0.3)
    return None


def cmd_scene_create(a):
    body = json.loads(a.json)
    sid = a.id
    res = rest("POST", f"/api/config/scene/config/{sid}", body)
    rest("POST", "/api/services/scene/reload", {})
    entity_id = _entity_by_attr("scene", "id", sid)
    out({"ok": True, "id": sid, "entity_id": entity_id, "result": res})


def cmd_script_create(a):
    body = json.loads(a.json)
    sid = a.id
    res = rest("POST", f"/api/config/script/config/{sid}", body)
    rest("POST", "/api/services/script/reload", {})
    out({"ok": True, "id": sid, "entity_id": f"script.{sid}", "result": res})


# --- raw config files -----------------------------------------------------
# Routed through the ha_copilot tool API (read_config_file / write_config_file)
# so file access works against any deployment — Docker, bare metal, or WSL —
# instead of assuming a local WSL distro on the operator's machine.
def _run_tool(tool: str, args: dict):
    res = rest("POST", "/api/ha_copilot/run_tool", {"tool": tool, "args": args})
    result = res.get("result", res) if isinstance(res, dict) else res
    if isinstance(result, dict) and result.get("error"):
        raise SystemExit(result["error"])
    return result


def cmd_conf_get(a):
    if ".." in pathlib.PurePosixPath(a.path).parts:
        raise SystemExit("path traversal not allowed")
    res = _run_tool("read_config_file", {"path": a.path})
    out({"path": a.path, "content": res.get("content", "")})


def cmd_conf_set(a):
    if ".." in pathlib.PurePosixPath(a.path).parts:
        raise SystemExit("path traversal not allowed")
    content = sys.stdin.read() if a.content == "-" else a.content
    _run_tool("write_config_file", {"path": a.path, "content": content})
    out({"path": a.path, "written": len(content)})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hactl", description="bottom-layer HA control for an AI operator"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, args=()):
        sp = sub.add_parser(name)
        for a_args, a_kwargs in args:
            sp.add_argument(*a_args, **a_kwargs)
        sp.set_defaults(fn=fn)
        return sp

    opt = lambda *a, **k: (a, k)  # noqa: E731

    # state plane
    add("states", cmd_states, [opt("--domain"), opt("--brief", action="store_true")])
    add("get", cmd_get, [opt("entity_id")])
    add("call", cmd_call, [opt("service"), opt("--data"), opt("--response", action="store_true")])
    add("template", cmd_template, [opt("template")])
    add("history", cmd_history, [opt("entity_id"), opt("--hours", type=int, default=24)])
    add("services", cmd_services, [opt("--domain")])
    add("error-log", cmd_errorlog)
    add("config", cmd_config)
    add("check", cmd_check)
    add("reload", cmd_reload, [opt("--domain")])

    # registry plane
    add("areas", cmd_areas)
    add("area-create", cmd_area_create, [opt("name")])
    add("entities", cmd_entities, [opt("--domain")])
    add("entity", cmd_entity, [opt("entity_id")])
    add(
        "entity-update",
        cmd_entity_update,
        [
            opt("entity_id"),
            opt("--name"),
            opt("--area"),
            opt("--new-id", dest="new_id"),
            opt("--disabled", choices=["true", "false"]),
        ],
    )
    add("devices", cmd_devices)
    add("device-update", cmd_device_update, [opt("device_id"), opt("--area"), opt("--name")])

    # config-editor plane
    add("automation-list", cmd_automation_list)
    add("automation-create", cmd_automation_create, [opt("id"), opt("json")])
    add("scene-create", cmd_scene_create, [opt("id"), opt("json")])
    add("script-create", cmd_script_create, [opt("id"), opt("json")])

    # raw config files
    add("conf-get", cmd_conf_get, [opt("path")])
    add("conf-set", cmd_conf_set, [opt("path"), opt("content")])
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

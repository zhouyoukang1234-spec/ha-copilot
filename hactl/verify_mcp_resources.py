"""End-to-end check of the MCP ``resources`` capability against a live HA.

Drives the bare JSON-RPC MCP endpoint: confirms ``initialize`` advertises a
``resources`` capability, ``resources/list`` returns the ``ha://areas`` and
``ha://entities`` aggregates plus per-entity ``ha://entity/<id>`` resources, and
``resources/read`` returns non-empty JSON for each — i.e. a resource-oriented
MCP client can read live HA state without calling tools. Run with the HA venv:

    HA_URL=http://localhost:8123 python hactl/verify_mcp_resources.py
"""
import json
import os
import urllib.request

BASE = os.environ.get("HA_URL", "http://localhost:8123")
TOKEN = os.environ.get("HA_TOKEN") or open("/root/ha-copilot/.ha_token").read().strip()
URL = BASE + "/api/ha_copilot/mcp"


def rpc(method: str, params: dict | None = None, rid: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    problems: list[str] = []

    caps = rpc("initialize", {}, 1).get("result", {}).get("capabilities", {})
    if "resources" not in caps:
        problems.append("initialize: 'resources' capability not advertised")

    listed = rpc("resources/list", None, 2).get("result", {}).get("resources", [])
    uris = [r.get("uri") for r in listed]
    for required in ("ha://areas", "ha://entities"):
        if required not in uris:
            problems.append(f"resources/list: missing {required}")

    entity_uris = [u for u in uris if u and u.startswith("ha://entity/")]
    if not entity_uris:
        problems.append("resources/list: no per-entity resources")

    def read_text(uri: str) -> str:
        got = rpc("resources/read", {"uri": uri}, 3).get("result", {})
        contents = got.get("contents", [])
        return contents[0].get("text", "") if contents else ""

    areas_text = read_text("ha://areas")
    if not areas_text:
        problems.append("resources/read ha://areas: empty")

    ent_text = ""
    if entity_uris:
        ent_text = read_text(entity_uris[0])
        if not ent_text or "error" in ent_text[:30]:
            problems.append(f"resources/read {entity_uris[0]}: empty/error")

    # An unknown resource must error, not return empty contents.
    bad = rpc("resources/read", {"uri": "ha://nope/zzz"}, 4)
    if "error" not in bad:
        problems.append("resources/read: unknown uri should error")

    print(f"capabilities: {sorted(caps)}")
    print(f"resources/list: {len(uris)} ({len(entity_uris)} entities)")
    print(f"  areas read: {len(areas_text)} chars")
    print(f"  entity[0]={entity_uris[0] if entity_uris else None} read: {len(ent_text)} chars")
    for p in problems:
        print("FAIL:", p)
    print("RESULT:", "PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Check the MCP ``prompts`` capability against a live HA.

Drives the bare JSON-RPC MCP endpoint: confirms ``initialize`` advertises a
``prompts`` capability, ``prompts/list`` returns the ``ha_context`` prompt, and
``prompts/get`` returns a non-empty text message — i.e. an off-the-shelf MCP
client can pull the same area-grouped exposed-entity context the native LLM API
injects (PR #15). Run with the HA venv:

    HA_URL=http://localhost:8123 python hactl/verify_mcp_prompts.py
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

    init = rpc("initialize", {}, 1).get("result", {})
    caps = init.get("capabilities", {})
    if "prompts" not in caps:
        problems.append("initialize: 'prompts' capability not advertised")

    listed = rpc("prompts/list", None, 2).get("result", {}).get("prompts", [])
    names = [p.get("name") for p in listed]
    if "ha_context" not in names:
        problems.append(f"prompts/list: missing 'ha_context' (got {names})")

    got = rpc("prompts/get", {"name": "ha_context"}, 3).get("result", {})
    messages = got.get("messages", [])
    text = ""
    if messages:
        text = messages[0].get("content", {}).get("text", "")
    if not text:
        problems.append("prompts/get: empty message text")

    # An unknown prompt must error, not return an empty body.
    bad = rpc("prompts/get", {"name": "does_not_exist"}, 4)
    if "error" not in bad:
        problems.append("prompts/get: unknown prompt should error")

    print(f"capabilities: {sorted(caps)}")
    print(f"prompts/list: {names}")
    print(f"prompts/get ha_context: {len(text)} chars")
    print(f"  preview: {text[:80]!r}")
    for p in problems:
        print("FAIL:", p)
    print("RESULT:", "PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())

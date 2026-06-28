"""Home Assistant API + native ha_copilot MCP helper (local HA on this VM)."""
import json
import os
import urllib.parse
import urllib.request

BASE = "http://localhost:8123"
CID = BASE + "/"


def _post_json(path, body, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode()[:400]}


def _post_form(path, body):
    req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(body).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode()[:400]}


def login(user="admin", password="devinpass123"):
    flow = _post_json("/auth/login_flow", {"client_id": CID, "handler": ["homeassistant", None], "redirect_uri": CID})
    res = _post_json(f"/auth/login_flow/{flow['flow_id']}", {"username": user, "password": password, "client_id": CID})
    if res.get("type") != "create_entry":
        raise RuntimeError("login failed: " + json.dumps(res)[:300])
    tok = _post_form("/auth/token", {"grant_type": "authorization_code", "code": res["result"], "client_id": CID})
    if "access_token" not in tok:
        raise RuntimeError("token failed: " + json.dumps(tok)[:300])
    open("/home/ubuntu/ha_access.txt", "w").write(tok["access_token"])
    return tok["access_token"]


def token():
    if os.path.exists("/home/ubuntu/ha_access.txt"):
        t = open("/home/ubuntu/ha_access.txt").read().strip()
        if t:
            return t
    return login()


def H():
    return {"Authorization": "Bearer " + token(), "Content-Type": "application/json"}


def get(path):
    req = urllib.request.Request(BASE + path, headers=H())
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def mcp(method, params=None, _id=1):
    """Call the native ha_copilot MCP JSON-RPC endpoint."""
    body = {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}
    req = urllib.request.Request(BASE + "/api/ha_copilot/mcp", data=json.dumps(body).encode(),
        headers=H(), method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=120).read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode()[:600]}


def tool(name, **args):
    """Invoke an MCP tool by name."""
    r = mcp("tools/call", {"name": name, "arguments": args})
    if "result" in r and "content" in r["result"]:
        out = []
        for c in r["result"]["content"]:
            if c.get("type") == "text":
                try:
                    out.append(json.loads(c["text"]))
                except Exception:
                    out.append(c["text"])
        return out[0] if len(out) == 1 else out
    return r


if __name__ == "__main__":
    print("token:", token()[:20], "...")
    cfg = get("/api/config")
    print("HA", cfg["version"], "| state", cfg["state"], "| name", cfg["location_name"])

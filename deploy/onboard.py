"""Non-interactive Home Assistant onboarding for the dev harness.

Creates the owner user, finishes the onboarding steps and writes an access
token to $HA_TOKEN_FILE (default: ./ha_token.txt). Idempotent: if HA is already
onboarded it just exits 0 so the harness can be re-run safely.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("HA_BASE", "http://localhost:8123")
CLIENT_ID = BASE + "/"
USERNAME = os.environ.get("HA_USERNAME", "devin")
PASSWORD = os.environ.get("HA_PASSWORD", "devin-ha-2026")
TOKEN_FILE = os.environ.get("HA_TOKEN_FILE", "ha_token.txt")


def req(path, body=None, token=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())


def token_from_code(code):
    form = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "code": code,
        "grant_type": "authorization_code",
    }).encode()
    r = urllib.request.Request(BASE + "/auth/token", data=form,
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())["access_token"]


def login_token():
    """Mint a token via the username/password auth flow (already-onboarded HA)."""
    flow = req("/auth/login_flow", {
        "client_id": CLIENT_ID,
        "handler": ["homeassistant", None],
        "redirect_uri": CLIENT_ID,
    })
    step = req(f"/auth/login_flow/{flow['flow_id']}", {
        "client_id": CLIENT_ID,
        "username": USERNAME,
        "password": PASSWORD,
    })
    if step.get("type") != "create_entry":
        raise SystemExit(f"login failed: {step}")
    return token_from_code(step["result"])


try:
    u = req("/api/onboarding/users", {
        "client_id": CLIENT_ID,
        "name": "Devin",
        "username": USERNAME,
        "password": PASSWORD,
        "language": "en",
    })
    print("user created")
    access = token_from_code(u["auth_code"])
except urllib.error.HTTPError as e:
    if e.code in (403, 404):
        print("already onboarded; logging in for a token")
        access = login_token()
    else:
        raise
print("access token obtained")

for path, body in [
    ("/api/onboarding/core_config", {}),
    ("/api/onboarding/analytics", {}),
    ("/api/onboarding/integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}),
]:
    try:
        req(path, body, token=access)
        print("step", path, "ok")
    except Exception as e:  # noqa: BLE001 - best-effort, some steps may already be done
        print("step", path, "->", e)

with open(TOKEN_FILE, "w") as f:
    f.write(access)
print("saved access token to", TOKEN_FILE)

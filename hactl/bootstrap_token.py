"""One-off: mint a long-lived access token for hactl and save it.

Run once. Uses the admin login flow to obtain a short-lived token, then mints
a long-lived access token over the WebSocket API and writes it to TOKEN_FILE.
"""

import asyncio
import json
import pathlib
import sys

import requests
import websockets

BASE = "http://localhost:8123"
WS = "ws://localhost:8123/api/websocket"
CLIENT = "http://localhost:8123/"
USER = "aodao"
PASSWORD = "daodao123"
TOKEN_FILE = pathlib.Path(__file__).resolve().parent.parent / ".ha_token"


def short_lived_token() -> str:
    s = requests.Session()
    flow = s.post(
        f"{BASE}/auth/login_flow",
        json={
            "client_id": CLIENT,
            "handler": ["homeassistant", None],
            "redirect_uri": CLIENT,
        },
    ).json()
    step = s.post(
        f"{BASE}/auth/login_flow/{flow['flow_id']}",
        json={"client_id": CLIENT, "username": USER, "password": PASSWORD},
    ).json()
    if step.get("type") != "create_entry":
        raise SystemExit(f"login failed: {step}")
    tok = s.post(
        f"{BASE}/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": step["result"],
            "client_id": CLIENT,
        },
    ).json()
    return tok["access_token"]


async def mint_llat(access_token: str) -> str:
    async with websockets.connect(WS, max_size=None) as ws:
        assert json.loads(await ws.recv())["type"] == "auth_required"
        await ws.send(json.dumps({"type": "auth", "access_token": access_token}))
        if json.loads(await ws.recv())["type"] != "auth_ok":
            raise SystemExit("ws auth failed")
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "auth/long_lived_access_token",
                    "client_name": "hactl",
                    "lifespan": 3650,
                }
            )
        )
        res = json.loads(await ws.recv())
        if not res.get("success"):
            raise SystemExit(f"llat failed: {res}")
        return res["result"]


def main() -> None:
    access = short_lived_token()
    llat = asyncio.run(mint_llat(access))
    TOKEN_FILE.write_text(llat, encoding="utf-8")
    print(f"long-lived token written to {TOKEN_FILE} ({len(llat)} chars)")


if __name__ == "__main__":
    sys.exit(main())

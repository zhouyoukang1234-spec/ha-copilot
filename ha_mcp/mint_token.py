"""Mint a long-lived Home Assistant access token for the bridge.

Reads a short-lived token (from onboarding) at $HA_TOKEN_FILE or argv[1] and
exchanges it via the WebSocket API for a durable long-lived token, written to
$HA_LLAT_FILE (default ha_llat.txt). Run once after onboarding.
"""
import asyncio
import os
import sys

import aiohttp

BASE = os.environ.get("HA_BASE_URL", "http://localhost:8123")
SRC = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HA_TOKEN_FILE", "ha_token.txt")
OUT = os.environ.get("HA_LLAT_FILE", "ha_llat.txt")
NAME = os.environ.get("HA_LLAT_NAME", "devin-ha-mcp")


async def main() -> None:
    token = open(SRC).read().strip()
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(BASE + "/api/websocket") as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": token})
            r = await ws.receive_json()
            if r.get("type") != "auth_ok":
                raise SystemExit(f"auth failed: {r}")
            await ws.send_json({"id": 1, "type": "auth/long_lived_access_token",
                                "client_name": NAME, "lifespan": 3650})
            r = await ws.receive_json()
            if not r.get("success"):
                raise SystemExit(f"mint failed: {r}")
            open(OUT, "w").write(r["result"])
            print(f"wrote long-lived token to {OUT} ({len(r['result'])} chars)")


if __name__ == "__main__":
    asyncio.run(main())

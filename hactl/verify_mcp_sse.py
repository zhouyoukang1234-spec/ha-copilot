"""End-to-end check of the standard MCP HTTP+SSE transport against a live HA.

Opens ``GET /api/ha_copilot/mcp/sse``, reads the announced ``endpoint`` event,
then POSTs ``initialize`` / ``tools/list`` / ``tools/call`` to it and reads each
JSON-RPC reply back over the SSE stream — exactly how an off-the-shelf MCP
client (Claude Desktop, Cline, ...) drives the server. Run with the HA venv:

    HA_URL=http://localhost:8123 python hactl/verify_mcp_sse.py
"""
import asyncio
import json
import os

import aiohttp

BASE = os.environ.get("HA_URL", "http://localhost:8123")
TOKEN = os.environ.get("HA_TOKEN") or open("/root/ha-copilot/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


async def run() -> int:
    async with aiohttp.ClientSession() as session:
        resp = await session.get(BASE + "/api/ha_copilot/mcp/sse", headers=HEADERS)
        if resp.status != 200 or not resp.headers.get("Content-Type", "").startswith(
            "text/event-stream"
        ):
            print(f"SSE open failed: status={resp.status}")
            return 1

        endpoint: str | None = None
        messages: list[dict] = []

        async def reader() -> None:
            nonlocal endpoint
            event = None
            async for raw in resp.content:
                line = raw.decode().rstrip("\r\n")
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data = line.split(":", 1)[1].strip()
                    if event == "endpoint":
                        endpoint = data
                    elif event == "message":
                        messages.append(json.loads(data))
                elif line == "":
                    event = None

        reader_task = asyncio.create_task(reader())
        for _ in range(80):
            if endpoint:
                break
            await asyncio.sleep(0.1)
        if not endpoint:
            print("no endpoint event received")
            reader_task.cancel()
            return 1

        url = BASE + endpoint

        async def call(payload: dict) -> dict:
            before = len(messages)
            post = await session.post(url, headers=HEADERS, json=payload)
            if post.status != 202:
                raise RuntimeError(f"POST status {post.status}")
            for _ in range(200):
                if len(messages) > before:
                    return messages[-1]
                await asyncio.sleep(0.05)
            raise RuntimeError("no SSE reply for " + payload.get("method", "?"))

        init = await call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        tools_list = await call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        call_res = await call(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_states", "arguments": {"domain": "light"}},
            }
        )
        reader_task.cancel()

        count = len(tools_list["result"]["tools"])
        is_error = call_res["result"]["isError"]
        print(f"initialize: {init['result']['protocolVersion']} / "
              f"{init['result']['serverInfo']['name']}")
        print(f"tools/list: {count}")
        print(f"tools/call list_states isError: {is_error}")
        ok = count > 0 and not is_error
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))

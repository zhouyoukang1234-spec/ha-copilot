"""Drive a live HA via the HA-MCP bridge to build durable, user-visible artifacts:
an admin user, a working automation, a helper switch and a custom dashboard.
Everything here is done as MCP tool calls - the agent operating HA directly.
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def call(s, tool, **args):
    res = await s.call_tool(tool, args)
    if res.isError:
        raise RuntimeError(f"{tool}: {res.content[0].text if res.content else ''}")
    sc = res.structuredContent
    if isinstance(sc, dict):
        return sc["result"] if set(sc.keys()) == {"result"} else sc
    blocks = [json.loads(c.text) for c in res.content if hasattr(c, "text")]
    return (blocks[0] if len(blocks) == 1 else blocks) if blocks else None


async def main():
    p = StdioServerParameters(command=sys.executable, args=["-m", "ha_mcp.server"], env={**os.environ})
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            # 1) Admin UI user (so we can log into the frontend deterministically)
            users = await call(s, "ha_ws", command_type="config/auth/list")
            existing = next((u for u in users if u.get("name") == "Devin Demo"), None)
            if existing:
                uid = existing["id"]
            else:
                created = await call(s, "ha_ws", command_type="config/auth/create",
                                     payload=json.dumps({"name": "Devin Demo"}))
                uid = created["user"]["id"]
            await call(s, "ha_ws", command_type="config/auth/update",
                       payload=json.dumps({"user_id": uid, "group_ids": ["system-admin"]}))
            try:
                await call(s, "ha_ws", command_type="config/auth_provider/homeassistant/create",
                           payload=json.dumps({"user_id": uid, "username": "devindemo",
                                               "password": "DevinDemo2026!"}))
                print(f"ensured admin user devindemo ({uid})")
            except RuntimeError as e:
                print(f"credential already present ({uid}): {str(e)[:60]}")

            # 2) Helper switch the automation will react to
            states = await call(s, "list_states", domain="input_boolean")
            have_switch = any(st["entity_id"] == "input_boolean.devin_demo_switch" for st in states)
            if not have_switch:
                await call(s, "create_helper", helper_domain="input_boolean",
                           config=json.dumps({"name": "Devin Demo Switch", "icon": "mdi:robot"}))
                print("created input_boolean.devin_demo_switch")
            else:
                print("switch already exists")

            # 3) A real working automation: switch ON -> living-room light ON; OFF -> OFF
            await call(s, "save_automation", config_id="7001", config=json.dumps({
                "alias": "Devin Demo: switch drives living-room light",
                "trigger": [{"platform": "state", "entity_id": "input_boolean.devin_demo_switch"}],
                "action": [{
                    "choose": [{
                        "conditions": [{"condition": "state",
                                        "entity_id": "input_boolean.devin_demo_switch", "state": "on"}],
                        "sequence": [{"service": "light.turn_on",
                                      "target": {"entity_id": "light.ke_ting_deng"}}],
                    }],
                    "default": [{"service": "light.turn_off",
                                 "target": {"entity_id": "light.ke_ting_deng"}}],
                }],
            }))
            print("saved automation 7001")

            # 4) A custom 'Devin Console' dashboard (url_path must contain a hyphen)
            url = "devin-console"
            dashes = await call(s, "list_dashboards")
            if not any(d.get("url_path") == url for d in dashes):
                await call(s, "ha_ws", command_type="lovelace/dashboards/create",
                           payload=json.dumps({"url_path": url, "title": "Devin Console",
                                               "show_in_sidebar": True, "require_admin": False,
                                               "mode": "storage", "icon": "mdi:robot-happy"}))
                print("created dashboard /devin")
            await call(s, "save_dashboard", url_path=url, config=json.dumps({
                "title": "Devin Console",
                "views": [{
                    "title": "Home", "path": "home", "icon": "mdi:home-assistant",
                    "cards": [
                        {"type": "markdown",
                         "content": "# 由 Devin 经 MCP 全权操控\n这个仪表盘、下面的自动化与开关，全部由 AI 通过 HA-MCP 接口创建。拨动开关即触发自动化。"},
                        {"type": "entities", "title": "控制", "entities": [
                            {"entity": "input_boolean.devin_demo_switch", "name": "Devin 演示开关"},
                            {"entity": "light.ke_ting_deng", "name": "客厅灯"},
                            {"entity": "light.men_lang_deng", "name": "门廊灯"},
                        ]},
                        {"type": "glance", "title": "全部灯", "entities": [
                            "light.ke_ting_deng", "light.men_lang_deng",
                        ]},
                    ],
                }],
            }))
            print("wrote dashboard config")
            print("DONE")


if __name__ == "__main__":
    asyncio.run(main())

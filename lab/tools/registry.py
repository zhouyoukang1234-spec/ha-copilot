#!/usr/bin/env python3
"""注册表 / 区域管理 (Registry Toolkit) — 替代用户最繁琐的 UI 整理工作。

通过 HA WebSocket API 编程操作 **区域注册表 / 实体注册表 / 设备注册表**：
创建区域、把实体/设备归位到区域、改名、贴标签、批量按规则归类。
这些在 HA 里通常是用户在设置界面里一个个手点的活，本工具让 agent 一次性完成。

用法（库）：
    import asyncio
    from registry import Registry
    async def go():
        async with Registry(token) as r:
            await r.ensure_areas(["主卧", "次卧"])
            stats = await r.assign_by_rules(RULES)
    asyncio.run(go())
"""

from __future__ import annotations

import json

import websockets


class Registry:
    def __init__(self, token: str, url: str = "ws://127.0.0.1:8123/api/websocket") -> None:
        self.token = token
        self.url = url
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._id = 0

    async def __aenter__(self) -> Registry:
        self._ws = await websockets.connect(self.url, max_size=None)
        await self._ws.recv()  # auth_required
        await self._ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        msg = json.loads(await self._ws.recv())
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth failed: {msg}")
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def _call(self, msg_type: str, **kwargs) -> dict:
        assert self._ws is not None
        self._id += 1
        mid = self._id
        await self._ws.send(json.dumps({"id": mid, "type": msg_type, **kwargs}))
        while True:
            msg = json.loads(await self._ws.recv())
            if msg.get("id") == mid:
                if not msg.get("success", True):
                    raise RuntimeError(f"{msg_type} failed: {msg.get('error')}")
                return msg.get("result")

    # ---- areas ----
    async def areas(self) -> list[dict]:
        return await self._call("config/area_registry/list")

    async def create_area(self, name: str) -> dict:
        return await self._call("config/area_registry/create", name=name)

    async def ensure_areas(self, names: list[str]) -> dict[str, str]:
        existing = {a["name"]: a["area_id"] for a in await self.areas()}
        for name in names:
            if name not in existing:
                area = await self.create_area(name)
                existing[name] = area["area_id"]
        return existing

    # ---- entities ----
    async def entities(self) -> list[dict]:
        return await self._call("config/entity_registry/list")

    async def update_entity(self, entity_id: str, **changes) -> dict:
        return await self._call("config/entity_registry/update",
                                entity_id=entity_id, **changes)

    async def assign_entity(self, entity_id: str, area_id: str) -> dict:
        return await self.update_entity(entity_id, area_id=area_id)

    # ---- devices ----
    async def devices(self) -> list[dict]:
        return await self._call("config/device_registry/list")

    async def assign_device(self, device_id: str, area_id: str) -> dict:
        return await self._call("config/device_registry/update",
                                device_id=device_id, area_id=area_id)

    # ---- bulk ----
    async def assign_by_rules(self, rules: list[tuple[str, list[str]]],
                              only_unassigned: bool = True) -> dict:
        """rules: [(area_name, [keywords])]; match entity_id/name, first hit wins."""
        area_ids = await self.ensure_areas([r[0] for r in rules])
        ents = await self.entities()
        stats: dict[str, int] = {r[0]: 0 for r in rules}
        stats["_skipped"] = 0
        stats["_unmatched"] = 0
        for ent in ents:
            if only_unassigned and ent.get("area_id"):
                stats["_skipped"] += 1
                continue
            hay = f"{ent.get('entity_id', '')} {ent.get('original_name') or ''} {ent.get('name') or ''}".lower()
            placed = False
            for area_name, keywords in rules:
                if any(k.lower() in hay for k in keywords):
                    await self.assign_entity(ent["entity_id"], area_ids[area_name])
                    stats[area_name] += 1
                    placed = True
                    break
            if not placed:
                stats["_unmatched"] += 1
        return stats

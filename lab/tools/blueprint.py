#!/usr/bin/env python3
"""蓝图域工具 (Blueprint Toolkit) — 导入社区蓝图 + 读取输入定义 + 实例化自动化。

蓝图是 HA 社区复用自动化/脚本的标准载体。本工具让 agent：
导入任意蓝图 URL → 读取它声明的输入(input)定义 → 用真实实体填充 → 实例化为自动化。
配合 author.create_blueprint_automation 即可"对话式套用社区最佳实践"。

    async with Blueprints(token) as bp:
        await bp.import_url("https://community.../motion_light.yaml")
        defs = await bp.inputs("automation", "homeassistant/motion_light.yaml")
"""

from __future__ import annotations

import json

import websockets


class Blueprints:
    def __init__(self, token: str, url: str = "ws://127.0.0.1:8123/api/websocket") -> None:
        self.token = token
        self.url = url
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._id = 0

    async def __aenter__(self) -> Blueprints:
        self._ws = await websockets.connect(self.url, max_size=None)
        await self._ws.recv()
        await self._ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        if json.loads(await self._ws.recv()).get("type") != "auth_ok":
            raise RuntimeError("WS auth failed")
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

    async def list(self, domain: str) -> dict:
        return await self._call("blueprint/list", domain=domain)

    async def import_url(self, url: str) -> dict:
        """从 URL 预览蓝图(不落盘)。返回 suggested_filename + blueprint。"""
        return await self._call("blueprint/import", url=url)

    async def save(self, domain: str, path: str, yaml_text: str,
                   url: str | None = None) -> dict:
        kwargs = {"domain": domain, "path": path, "yaml": yaml_text}
        if url:
            kwargs["source_url"] = url
        return await self._call("blueprint/save", **kwargs)

    async def inputs(self, domain: str, path: str) -> dict:
        """读取某蓝图声明的输入定义(供填充)。"""
        listing = await self.list(domain)
        meta = listing.get(path)
        if not meta:
            raise KeyError(f"blueprint not found: {domain}/{path}")
        return (meta.get("metadata") or {}).get("input") or {}

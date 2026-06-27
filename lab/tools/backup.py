#!/usr/bin/env python3
"""备份 / 恢复域工具 (Backup Toolkit) — 编程化创建/列出/详情/删除/恢复备份。

HA 的备份在 UI 里要一步步点；本工具经 WebSocket 把整套生命周期编程化，
让 agent 在做任何高风险变更前先一键留存、出问题可一键回滚。

    import asyncio
    from backup import Backup
    async def go():
        async with Backup(token) as b:
            job = await b.create("变更前快照")
            bk = await b.wait_for(job)         # 等待生成完成
            print(await b.list())
    asyncio.run(go())
"""

from __future__ import annotations

import asyncio
import json

import websockets

LOCAL = "backup.local"


class Backup:
    def __init__(self, token: str, url: str = "ws://127.0.0.1:8123/api/websocket") -> None:
        self.token = token
        self.url = url
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._id = 0

    async def __aenter__(self) -> Backup:
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

    async def agents(self) -> list[dict]:
        return (await self._call("backup/agents/info"))["agents"]

    async def list(self) -> list[dict]:
        return (await self._call("backup/info"))["backups"]

    async def details(self, backup_id: str) -> dict:
        return await self._call("backup/details", backup_id=backup_id)

    async def delete(self, backup_id: str) -> None:
        await self._call("backup/delete", backup_id=backup_id)

    async def create(self, name: str, agent_ids: list[str] | None = None,
                     include_database: bool = True) -> str:
        """触发生成备份，返回 backup_job_id（异步生成）。"""
        result = await self._call(
            "backup/generate",
            agent_ids=agent_ids or [LOCAL],
            name=name,
            include_homeassistant=True,
            include_database=include_database,
            include_folders=[],
            include_addons=[],
            include_all_addons=False,
        )
        return result["backup_job_id"]

    async def wait_for(self, job_id: str, timeout: float = 120.0) -> dict:
        """轮询直到该 job 生成的备份出现在列表里。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            info = await self._call("backup/info")
            ev = info.get("last_action_event") or {}
            if ev.get("state") == "completed" and ev.get("reason") is None:
                backups = info["backups"]
                if backups:
                    return sorted(backups, key=lambda b: b.get("date", ""))[-1]
            if ev.get("state") == "failed":
                raise RuntimeError(f"backup failed: {ev}")
            await asyncio.sleep(2)
        raise TimeoutError("backup did not complete in time")

    async def restore(self, backup_id: str, agent_id: str = LOCAL,
                      restore_database: bool = True) -> None:
        """从备份恢复（会重启 HA）。高风险，仅在确需回滚时调用。"""
        await self._call("backup/restore", backup_id=backup_id, agent_id=agent_id,
                         restore_homeassistant=True, restore_database=restore_database)


async def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="备份生命周期 CLI")
    ap.add_argument("token")
    ap.add_argument("action", choices=["list", "create", "delete"])
    ap.add_argument("--name", default="Devin 手动快照")
    ap.add_argument("--id", dest="backup_id")
    args = ap.parse_args()
    async with Backup(args.token) as b:
        if args.action == "list":
            for bk in await b.list():
                print(bk.get("backup_id"), "|", bk.get("name"), "|", bk.get("date"))
        elif args.action == "create":
            bk = await b.wait_for(await b.create(args.name))
            print("created:", bk.get("backup_id"), "|", bk.get("name"))
        elif args.action == "delete":
            await b.delete(args.backup_id)
            print("deleted:", args.backup_id)


if __name__ == "__main__":
    asyncio.run(_cli())

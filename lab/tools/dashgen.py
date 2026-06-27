#!/usr/bin/env python3
"""按区域自动生成前端仪表盘 (Dashboard Generator) — 替用户设计 UI。

读取注册表(区域 + 实体归属) → 自动产出一个多视图 Lovelace 仪表盘：
每个区域一个视图(实体按域分组成卡片)，外加一个能源视图。
用户原本要在 UI 里逐卡拖拽编排；本工具一次成型，且随注册表变化可重新生成。

    python dashgen.py <HA_TOKEN>
"""

from __future__ import annotations

import argparse
import asyncio

from author import Authoring
from registry import Registry

# 域 → (中文标题, 图标)
DOMAIN_META = {
    "light": ("灯光", "mdi:lightbulb-group"),
    "switch": ("开关", "mdi:toggle-switch"),
    "fan": ("风扇", "mdi:fan"),
    "climate": ("空调", "mdi:thermostat"),
    "media_player": ("影音", "mdi:speaker"),
    "sensor": ("传感器", "mdi:gauge"),
    "binary_sensor": ("状态", "mdi:checkbox-marked-circle"),
    "cover": ("窗帘", "mdi:window-shutter"),
    "lock": ("门锁", "mdi:lock"),
    "camera": ("摄像头", "mdi:cctv"),
    "number": ("数值", "mdi:numeric"),
    "select": ("选择", "mdi:form-dropdown"),
}


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


async def build(token: str, config_dir: str) -> dict:
    async with Registry(token) as reg:
        areas = await reg.areas()
        ents = await reg.entities()
    area_name = {a["area_id"]: a["name"] for a in areas}
    by_area: dict[str, list[str]] = {}
    for e in ents:
        aid = e.get("area_id")
        if not aid or e.get("disabled_by") or e.get("hidden_by"):
            continue
        by_area.setdefault(aid, []).append(e["entity_id"])

    views = []
    for aid, eids in sorted(by_area.items(), key=lambda kv: -len(kv[1])):
        # 按域分组成卡片
        groups: dict[str, list[str]] = {}
        for eid in sorted(eids):
            groups.setdefault(_domain(eid), []).append(eid)
        cards = []
        for dom, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            title, icon = DOMAIN_META.get(dom, (dom, "mdi:shape"))
            cards.append({
                "type": "entities", "title": f"{title} ({len(items)})",
                "icon": icon, "entities": items,
            })
        views.append({
            "title": area_name.get(aid, aid),
            "path": f"area-{aid}",
            "cards": cards,
        })

    # 能源视图
    views.append({
        "title": "能源", "path": "energy", "icon": "mdi:lightning-bolt",
        "cards": [{"type": "energy-distribution"},
                  {"type": "energy-usage-graph"}],
    })

    a = Authoring(config_dir, token=token)
    path = a.generate_dashboard("devin_area_dashboard.yaml",
                                "Devin 区域总台", views)
    return {"views": len(views), "areas": len(by_area),
            "entities": sum(len(v) for v in by_area.values()), "path": path}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("token")
    ap.add_argument("--config", default="/home/ubuntu/ha_lab/config")
    args = ap.parse_args()
    res = await build(args.token, args.config)
    print(res)


if __name__ == "__main__":
    asyncio.run(main())
